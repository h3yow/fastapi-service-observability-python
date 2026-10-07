"""Shared test helpers: clients, raw-ASGI calls and exposition parsing."""
from __future__ import annotations

import contextlib
import re
import typing

import httpx
import pytest

from observability.factory import create_app
from observability.metrics import DEFAULT_MAX_SERIES
from observability.setup import get_registry

if typing.TYPE_CHECKING:
    from fastapi import FastAPI


def make_app(
    *,
    request_timeout: float | None = 5.0,
    enabled: bool = True,
    duration_enabled: bool = True,
    max_series: int | None = DEFAULT_MAX_SERIES,
) -> "FastAPI":
    return create_app(
        request_timeout=request_timeout,
        enabled=enabled,
        duration_enabled=duration_enabled,
        max_series=max_series,
    )


def registry_of(app: "FastAPI"):
    reg = get_registry(app)
    assert reg is not None
    return reg


@contextlib.asynccontextmanager
async def asgi_client(app: "FastAPI", *, raise_app_exceptions: bool = False):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


def _http_scope(path: str = "/", *, method: str = "GET", query: str = "") -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "headers": [],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
    }


async def raw_asgi_request(
    app: "FastAPI",
    path: str,
    *,
    method: str = "GET",
    query: str = "",
    disconnect_after: float | None = None,
):
    """Drive the app with a raw ASGI request.

    When ``disconnect_after`` is set, an ``http.disconnect`` message is
    delivered that many seconds after the request message, modelling a client
    that goes away mid-request (the ASGI test transport cannot do this).
    Returns ``(messages_sent, elapsed_seconds)``.
    """
    import asyncio
    import time

    messages: list[dict] = []

    async def receive() -> dict:
        if not getattr(receive, "_used", False):
            receive._used = True  # type: ignore[attr-defined]
            return {"type": "http.request", "body": b"", "more_body": False}
        if disconnect_after is not None:
            await asyncio.sleep(disconnect_after)
            return {"type": "http.disconnect"}
        await asyncio.sleep(3600)  # keep-alive: client simply waits
        raise AssertionError("unreachable")

    async def send(message: dict) -> None:
        messages.append(message)

    started = time.monotonic()
    await app(_http_scope(path, method=method, query=query), receive, send)
    return messages, time.monotonic() - started


_SAMPLE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)$")
_LABEL_RE = re.compile(r'(\w+)="((?:\\.|[^"\\])*)"')


def parse_samples(text: str) -> list[tuple[str, dict[str, str], float]]:
    """Parse Prometheus text format into ``(metric, labels, value)`` triples."""
    samples: list[tuple[str, dict[str, str], float]] = []
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        assert match is not None, f"unparseable metric line: {line!r}"
        name, _, raw_labels, raw_value = match.groups()
        labels = {key: val.replace(r'\"', '"').replace(r"\\", "\\")
                  for key, val in _LABEL_RE.findall(raw_labels or "")}
        value = float("inf" if raw_value == "+Inf" else "-inf" if raw_value == "-Inf" else raw_value)
        samples.append((name, labels, value))
    return samples


@pytest.fixture(autouse=True)
def _echo_section(request):
    print(f"\n===== {request.node.name} =====")
    yield
