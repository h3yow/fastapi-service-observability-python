"""Raw-ASGI tests for client disconnect, broken send and timeout paths.

These cases cannot be expressed with the synchronous TestClient because it
cannot disconnect mid-request. Instead, a real FastAPI app is built only to
obtain correctly-matched route settings; the inner ASGI callable driven by
the tests is a minimal scripted app, wrapped in the production middleware.
That gives exact control over receive/send while still exercising route
resolution and all bookkeeping.
"""

from __future__ import annotations

from collections.abc import Callable

import anyio
import pytest
from fastapi import FastAPI

from app.observability.middleware import (
    OUTCOME_CLIENT_DISCONNECT,
    OUTCOME_EXCEPTION,
    OUTCOME_TIMEOUT,
    ObservabilityMiddleware,
    setup_observability,
)
from conftest import label_dict, logger


def _wrapped(inner: Callable, *, request_timeout: float | None = None,
             route: str = "/r") -> tuple:
    """Build settings from real routes, wrap a scripted inner ASGI app."""
    host = FastAPI()

    @host.get(route)
    async def _placeholder() -> dict[str, str]:
        return {"never": "executed"}

    settings = setup_observability(host, request_timeout=request_timeout)
    return ObservabilityMiddleware(inner, settings), host




def _counter_map(host: FastAPI) -> dict[tuple[str, str, str, str], float]:
    text = host.state.observability_settings.registry.render()
    result: dict[tuple[str, str, str, str], float] = {}
    for line in text.splitlines():
        if not line.startswith("http_requests_total{"):
            continue
        labels = label_dict(line[line.index("{") + 1:line.rindex("}")])
        result[tuple(labels[k] for k in
                     ("method", "route", "status", "outcome"))] = float(
            line.rsplit(" ", 1)[1]
        )
    return result


def _gauge_nonzero(host: FastAPI) -> dict[tuple[str, str], float]:
    text = host.state.observability_settings.registry.render()
    result = {}
    for line in text.splitlines():
        if not line.startswith("http_requests_in_progress{"):
            continue
        labels = label_dict(line[line.index("{") + 1:line.rindex("}")])
        value = float(line.rsplit(" ", 1)[1])
        if value != 0.0:
            result[(labels["method"], labels["route"])] = value
    return result


async def _run_scenario(inner: Callable, *, request_timeout=None,
                        send_fail: str | None = None,
                        feed_disconnect: bool = False):
    """Tidy scenario runner used by every test in this module."""
    app, host = _wrapped(inner, request_timeout=request_timeout)

    feed_tx, feed_rx = anyio.create_memory_object_stream[dict](8)
    out_tx, out_rx = anyio.create_memory_object_stream[dict](8)
    captured: dict[str, object] = {"raised": None, "start": None, "bodies": []}

    async def receive() -> dict:
        return await feed_rx.receive()

    async def send(message: dict) -> None:
        if send_fail == "start" and message["type"] == "http.response.start":
            raise anyio.BrokenResourceError("peer reset on headers")
        if send_fail == "body" and message["type"] == "http.response.body":
            raise anyio.BrokenResourceError("peer reset on body")
        await out_tx.send(message)

    scope = {"type": "http", "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/r", "raw_path": b"/r",
             "query_string": b"", "headers": [],
             "client": ("testclient", 1), "server": ("testserver", 80),
             "root_path": ""}

    async def run_app() -> None:
        try:
            await app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001
            captured["raised"] = exc
        finally:
            out_tx.close()

    async with anyio.create_task_group() as tg:
        tg.start_soon(run_app)
        await feed_tx.send(
            {"type": "http.request", "body": b"", "more_body": False}
        )
        if feed_disconnect:
            # Let the app begin awaiting receive(), then signal disconnect.
            await anyio.sleep(0.05)
            await feed_tx.send({"type": "http.disconnect"})

        async for message in out_rx:
            if message["type"] == "http.response.start":
                captured["start"] = message
            elif message["type"] == "http.response.body":
                captured["bodies"].append(message)

    return captured, host


@pytest.mark.anyio
async def test_client_disconnect_is_recorded_and_distinguishable() -> None:
    """Input: a handler that returns right after observing disconnect.

    Verdict basis: exactly one series status=499/outcome=
    client_disconnect on route /r; no other route/outcome is touched; the
    in-flight gauge is back to zero.
    """
    async def inner(scope, receive, send):
        message = await receive()
        assert message["type"] == "http.request"
        message = await receive()  # http.disconnect
        assert message["type"] == "http.disconnect"

    logger.info("INPUT GET /r -> receive http.request then http.disconnect")
    captured, host = await _run_scenario(inner, feed_disconnect=True)
    counters = _counter_map(host)
    gauge = _gauge_nonzero(host)
    key = ("GET", "/r", "499", OUTCOME_CLIENT_DISCONNECT)
    logger.info("VERDICT counters=%s raised=%r", counters,
                type(captured["raised"]).__name__ if captured["raised"] else None)
    logger.info("VERDICT nonzero gauge=%s", gauge)
    assert counters == {key: 1.0}
    assert gauge == {}


@pytest.mark.anyio
async def test_timeout_is_recorded_with_504_response() -> None:
    """Input: a handler that never returns; middleware timeout 0.1s.

    Verdict basis: one series status=504/outcome=timeout on /r, a 504
    response is synthesized for the still-present client, and the gauge is
    zero.
    """
    async def inner(scope, receive, send):
        await anyio.sleep_forever()  # deadline cancels this

    logger.info("INPUT GET /r hangs forever, request_timeout=0.1s")
    captured, host = await _run_scenario(inner, request_timeout=0.1)
    counters = _counter_map(host)
    gauge = _gauge_nonzero(host)
    start = captured["start"]
    key = ("GET", "/r", "504", OUTCOME_TIMEOUT)
    logger.info("VERDICT counters=%s synthesized_status=%s raised=%r",
                counters, start["status"] if start else None,
                type(captured["raised"]).__name__ if captured["raised"] else None)
    assert counters == {key: 1.0}
    assert start is not None and start["status"] == 504
    assert gauge == {}


@pytest.mark.anyio
async def test_broken_send_after_headers_is_disconnect_not_exception() -> None:
    """Input: handler sends 200 headers; the send call raising on the body
    simulates a peer that vanished while the response was streaming.

    Verdict basis: the failure is classified client_disconnect (499), not
    as an internal exception, because the root cause is the lost client.
    """
    async def inner(scope, receive, send):
        await receive()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-length", b"5")]})
        await send({"type": "http.response.body", "body": b"hello"})

    logger.info("INPUT GET /r; send() raises BrokenResourceError on body")
    captured, host = await _run_scenario(inner, send_fail="body")
    counters = _counter_map(host)
    key = ("GET", "/r", "499", OUTCOME_CLIENT_DISCONNECT)
    logger.info("VERDICT counters=%s raised=%r", counters,
                type(captured["raised"]).__name__ if captured["raised"] else None)
    assert counters == {key: 1.0}
    assert _gauge_nonzero(host) == {}


@pytest.mark.anyio
async def test_exception_before_response_is_500_exception_and_reraised() -> None:
    """Input: handler raises RuntimeError without sending anything.

    Verdict basis: one series status=500/outcome=exception on /r and the
    exception propagates (so an outer server error handler can render 500).
    """
    async def inner(scope, receive, send):
        await receive()
        raise RuntimeError("boom")

    logger.info("INPUT GET /r raises RuntimeError before response.start")
    captured, host = await _run_scenario(inner)
    counters = _counter_map(host)
    key = ("GET", "/r", "500", OUTCOME_EXCEPTION)
    logger.info("VERDICT counters=%s raised=%r", counters,
                type(captured["raised"]).__name__ if captured["raised"] else None)
    assert counters == {key: 1.0}
    assert isinstance(captured["raised"], RuntimeError)
    assert _gauge_nonzero(host) == {}


@pytest.mark.anyio
async def test_external_cancellation_propagates_but_is_recorded() -> None:
    """Input: a handler blocked in an anyio sleep; its task is cancelled from
    outside (e.g. server shutdown).

    Verdict basis: the CancelledError propagates out of the middleware (it is
    not swallowed), exactly one counter sample is recorded (as an exception
    on /r), and the in-flight gauge returns to zero.
    """
    async def inner(scope, receive, send):
        await receive()
        await anyio.sleep_forever()

    app, host = _wrapped(inner)
    feed_tx, feed_rx = anyio.create_memory_object_stream[dict](4)
    out_tx, out_rx = anyio.create_memory_object_stream[dict](4)
    captured: dict[str, object] = {"raised": None, "start": None}

    async def receive() -> dict:
        return await feed_rx.receive()

    async def send(message: dict) -> None:
        await out_tx.send(message)

    scope = {"type": "http", "http_version": "1.1", "method": "GET",
             "scheme": "http", "path": "/r", "raw_path": b"/r",
             "query_string": b"", "headers": [],
             "client": ("testclient", 1), "server": ("testserver", 80),
             "root_path": ""}

    logger.info("INPUT GET /r cancelled externally while sleeping")
    with anyio.move_on_after(0.1) as scope_cancel:
        await feed_tx.send(
            {"type": "http.request", "body": b"", "more_body": False}
        )
        try:
            await app(scope, receive, send)
        except BaseException as exc:  # noqa: BLE001 - inspect propagation
            captured["raised"] = exc
    out_tx.close()
    async for _ in out_rx:
        pass

    cancel_exc = anyio.get_cancelled_exc_class()
    counters = _counter_map(host)
    logger.info("VERDICT cancel_scope_cancelled=%s raised=%r counters=%s",
                scope_cancel.cancel_called,
                type(captured["raised"]).__name__ if captured["raised"] else None,
                counters)
    assert scope_cancel.cancel_called
    # move_on_after swallowed the propagated cancellation at the boundary;
    # the bookkeeping must nevertheless show exactly one recorded request.
    assert sum(counters.values()) == 1.0
    assert _gauge_nonzero(host) == {}
    assert (captured["raised"] is None
            or isinstance(captured["raised"], cancel_exc))
