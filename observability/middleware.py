"""ASGI middleware that records every inbound HTTP request."""
from __future__ import annotations

from types import TracebackType
from typing import Any

from .registry import MetricsRegistry


class MetricsASGIMiddleware:
    def __init__(self, app: Any, registry: MetricsRegistry, *, request_timeout: float | None = 5.0) -> None:
        self.app = app
        self.registry = registry
        self.request_timeout = request_timeout

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        raise NotImplementedError
