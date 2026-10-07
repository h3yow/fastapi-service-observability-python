"""ASGI middleware that records every inbound HTTP request.

Guarantees
----------
* **Conservation.** Every request that enters the middleware increments the
  in-flight gauge exactly once and decrements it exactly once (``finally``),
  and contributes exactly one counter increment and one histogram observation
  when it leaves -- even on outer cancellation.  All updates go through the
  lock-protected registry, so concurrent requests to the same route cannot
  lose or duplicate counts, and recording cost is O(1) per request.
* **Route-templated labels.** The route label comes from the matched route's
  template (e.g. ``/items/{item_id}``); requests that match no route (404)
  collapse to ``__unmatched__``.  A method-not-allowed request (405) keeps the
  template of the path it *did* match -- either way the set of series stays
  bounded.
* **Distinguishable failure paths.** Every terminal request is assigned one
  fixed outcome (``ok`` / ``error`` / ``exception`` / ``timeout`` /
  ``client_disconnect``) using the documented precedence in
  :func:`observability.labels.classify_outcome`, and one dashboard-ready
  ``result`` class (``success`` / ``client_error`` / ``server_error`` /
  ``exception`` / ``timeout`` / ``client_disconnect``) via
  :func:`observability.labels.classify_result`.  Non-standard status codes
  collapse to ``status="other"`` instead of opening fresh series.
* **Cardinality safety lives in the registry.** Every recorded combination
  passes through the per-family cap, so an exhausted budget funnels this
  request into the single ``__overflow__`` fallback series; totals and
  histogram buckets stay conserved and the request is never dropped.

A request that produced a complete response is never relabelled as timeout or
client-disconnect even if such an event arrives afterwards, which prevents
keep-alive connection teardown from polluting the statistics.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import anyio
from starlette.routing import Match

from .labels import (
    ROUTE_UNMATCHED,
    classify_outcome,
    classify_result,
    normalize_method,
    normalize_route,
    normalize_status,
)
from .metrics import HTTP_INFLIGHT, HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL
from .registry import MetricsRegistry

logger = logging.getLogger("observability.metrics")

_ERROR_HEADERS = ((b"content-type", b"text/plain; charset=utf-8"),)

#: Terminal facts of one request: (status, raised, timed_out, disconnected).
_Facts = tuple[int | None, bool, bool, bool]


def _iter_matchable_routes(router: Any):
    """Yield leaf routes, descending into FastAPI included-router groups."""
    for route in getattr(router, "routes", None) or ():
        inner_router = getattr(route, "original_router", None)
        if inner_router is not None:
            # FastAPI 0.141 groups included routers behind an opaque wrapper.
            yield from _iter_matchable_routes(inner_router)
        else:
            yield route


def _resolve_route_label(router: Any, scope: dict) -> str:
    """Resolve the route template before the app runs (best-effort probe).

    Uses a shallow *probe* copy of the scope so that route matching (which
    populates ``path_params``) cannot mutate the real scope.  Partial matches
    (path matches but method does not -> 405) and absent matches (404) both
    collapse to :data:`ROUTE_UNMATCHED`, keeping label cardinality bounded.
    The post-hoc ``scope["route"]`` value (read after the app ran) is the
    authoritative source for the counter labels; this probe is needed for the
    in-flight gauge, which must be incremented up front.
    """
    probe: dict[str, Any] = dict(scope)
    for route in _iter_matchable_routes(router):
        matches = getattr(route, "matches", None)
        if matches is None:
            continue
        try:
            match, _ = matches(probe)
        except Exception:  # never let route probing break the request
            continue
        # FULL match -> use the template; PARTIAL (405) falls through and is
        # labelled as unmatched, just like NONE (404).
        if match == Match.FULL:
            return normalize_route(route)
    return ROUTE_UNMATCHED


def _post_route_label(scope: dict, pre_route: str) -> str:
    """Authoritative route label after the framework selected the route.

    The framework writes the matched route object into ``scope["route"]``
    (including for routes added via ``include_router``); this is more
    reliable than the up-front probe.  Nothing matched (404) leaves it unset,
    in which case the probe result (``__unmatched__``) is kept.
    """
    matched = scope.get("route")
    if matched is None:
        return pre_route
    return normalize_route(matched)


class MetricsASGIMiddleware:
    """Collect metrics for each inbound HTTP request."""

    def __init__(
        self,
        app: Any,
        registry: MetricsRegistry,
        *,
        router: Any | None = None,
        request_timeout: float | None = 5.0,
    ) -> None:
        self.app = app
        self.registry = registry
        # ``app`` is the next ASGI app in the stack; ``router`` is the root
        # FastAPI/Starlette application that owns the route table.
        self.router = router if router is not None else app
        self.request_timeout = request_timeout

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            # websocket/lifespan scopes are passed through untouched
            await self.app(scope, receive, send)
            return

        method = normalize_method(scope.get("method"))
        # Best-effort label resolved up front (needed for the in-flight gauge).
        pre_route = _resolve_route_label(self.router, scope)
        inflight_labels = (("method", method), ("route", pre_route))

        self.registry.inc_gauge(HTTP_INFLIGHT, inflight_labels, 1.0)
        started = anyio.current_time()
        recorded = False
        try:
            facts, held_exc = await self._invoke(scope, receive, send)
        except BaseException:
            # Outer cancellation (e.g. server shutdown) aborted the request:
            # still account for it exactly once, then let it propagate.
            self._record(method, _post_route_label(scope, pre_route),
                         (None, True, False, False), started)
            recorded = True
            raise
        finally:
            if not recorded:
                self._record(method, _post_route_label(scope, pre_route), facts, started)
            # guaranteed on every exit path -> the gauge can never leak
            self.registry.inc_gauge(HTTP_INFLIGHT, inflight_labels, -1.0)
        if held_exc is not None:
            # The framework already produced the error response; re-raise so
            # the server can log it, exactly like an uninstrumented app.
            raise held_exc

    def _record(self, method: str, route: str, facts: _Facts, started: float) -> None:
        status, raised, timed_out, disconnected = facts
        outcome = classify_outcome(
            status, raised=raised, timed_out=timed_out, disconnected=disconnected
        )
        result = classify_result(
            status, raised=raised, timed_out=timed_out, disconnected=disconnected
        )
        duration = anyio.current_time() - started
        self.registry.inc_counter(
            HTTP_REQUESTS_TOTAL,
            (
                ("method", method),
                ("route", route),
                ("status", normalize_status(status)),
                ("outcome", outcome),
                ("result", result),
            ),
            1.0,
        )
        self.registry.observe(
            HTTP_REQUEST_DURATION_SECONDS,
            (("method", method), ("route", route), ("outcome", outcome)),
            duration,
        )

    # ------------------------------------------------------------------ #
    async def _invoke(
        self, scope: dict, receive: Any, send: Any
    ) -> tuple[_Facts, BaseException | None]:
        """Run the wrapped app and return its terminal facts."""
        disconnected = anyio.Event()
        # Buffered so the reader keeps pulling client messages (including
        # http.disconnect) even when the application never calls receive, as
        # happens for ordinary GET handlers.  Servers apply flow control on
        # request bodies, so the buffer cannot grow without bound.
        to_app_send, to_app_recv = anyio.create_memory_object_stream[dict](16)
        app_cancel_scope = anyio.CancelScope()

        response_started = False
        status_holder: dict[str, int | None] = {"status": None}
        completed = False
        held_exc: BaseException | None = None

        async def wrapped_send(message: dict) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
                status_holder["status"] = int(message.get("status", 0))
            try:
                await send(message)
            except Exception:
                # The server could not deliver to the client: the peer is gone.
                disconnected.set()
                raise

        async def wrapped_receive() -> dict:
            try:
                return await to_app_recv.receive()
            except anyio.EndOfStream:
                return {"type": "http.disconnect"}

        async def reader() -> None:
            """Forward client messages; translate disconnect/errors to an event."""
            try:
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        disconnected.set()
                    await to_app_send.send(message)
            except anyio.get_cancelled_exc_class():
                raise
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                # The app side went away (timeout/completion); this is not a
                # client-disconnect signal and must not relabel the request.
                pass
            except Exception:
                # EndOfStream or any other receive failure: the client channel
                # is gone, treat it as a disconnect.
                disconnected.set()
            finally:
                with anyio.CancelScope(shield=True):
                    await to_app_send.aclose()

        async def watcher() -> None:
            await disconnected.wait()
            app_cancel_scope.cancel()

        async def run_app() -> None:
            nonlocal completed, held_exc
            try:
                with app_cancel_scope:
                    await self.app(scope, wrapped_receive, wrapped_send)
                if app_cancel_scope.cancelled_caught:
                    # Cancelled by the disconnect watcher: not a normal return.
                    return
                completed = True
            except anyio.get_cancelled_exc_class():
                # Absorbed only when our own scope cancelled it; an outer
                # cancellation (server shutdown) must keep propagating.
                if not app_cancel_scope.cancelled_caught:
                    raise
            except (KeyboardInterrupt, SystemExit):
                raise
            except BaseException as exc:  # endpoint error escaped the framework
                held_exc = exc
            finally:
                # App path is settled: stop reader/watcher.
                scope_group.cancel_scope.cancel()

        deadline = (
            anyio.current_time() + self.request_timeout
            if self.request_timeout is not None
            else math.inf
        )
        with anyio.CancelScope(deadline=deadline) as timeout_scope:
            async with anyio.create_task_group() as scope_group:
                scope_group.start_soon(reader)
                scope_group.start_soon(watcher)
                scope_group.start_soon(run_app)

        timed_out = bool(timeout_scope.cancelled_caught) and not completed
        disconnected_now = disconnected.is_set() and not completed
        raised = held_exc is not None
        if raised:
            logger.debug("unhandled exception while handling request", exc_info=held_exc)

        if not completed and not response_started:
            if timed_out:
                await self._emit(send, 504, b"Request Timeout", status_holder, disconnected)
            elif raised and not disconnected_now:
                await self._emit(send, 500, b"Internal Server Error", status_holder, disconnected)

        return (
            (status_holder["status"], raised, timed_out, disconnected_now),
            held_exc,
        )

    @staticmethod
    async def _emit(
        send: Any,
        status: int,
        body: bytes,
        status_holder: dict[str, int | None],
        disconnected: anyio.Event,
    ) -> None:
        """Best-effort fallback response for timeout/unhandled exception."""
        try:
            await send(
                {"type": "http.response.start", "status": status, "headers": list(_ERROR_HEADERS)}
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            status_holder["status"] = status
        except Exception:
            disconnected.set()
