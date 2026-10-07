"""Application factory with demo routes used to exercise the metrics."""
from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException

from .setup import setup_observability

logger = logging.getLogger("observability.demo")


def create_app(
    *,
    request_timeout: float | None = 5.0,
    duration_enabled: bool = True,
    max_series: int | None = 500,
    enabled: bool = True,
) -> FastAPI:
    """Build a fresh instrumented FastAPI application."""
    app = FastAPI(title="fastapi-service-observability")
    setup_observability(
        app,
        request_timeout=request_timeout,
        duration_enabled=duration_enabled,
        max_series=max_series,
        enabled=enabled,
    )

    @app.get("/")
    def read_root() -> dict[str, str]:
        return {"hello": "world"}

    @app.get("/items/{item_id}")
    def read_item(item_id: str) -> dict[str, str]:
        # Demonstrates that /items/1 and /items/42 share one route label.
        return {"item_id": item_id}

    @app.get("/slow")
    async def slow(wait: float = 10.0) -> dict[str, float]:
        # Sleeps until cancelled: exceeds the request timeout -> "timeout".
        await asyncio.sleep(wait)
        return {"waited": wait}

    @app.get("/boom")
    def boom() -> None:
        # Escapes the framework -> "exception" outcome + 500 response.
        raise RuntimeError("deliberate failure")

    @app.get("/http-error")
    def http_error() -> None:
        # A normal error response: outcome "error", status "503".
        raise HTTPException(status_code=503, detail="unavailable")

    return app
