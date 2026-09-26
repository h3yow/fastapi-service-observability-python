"""Application factory and demo endpoints exercising normal/error paths."""

from __future__ import annotations

import asyncio
import os

from fastapi import FastAPI, HTTPException

from .observability.middleware import setup_observability

# Per-request timeout applied by the observability middleware. ``None``
# disables it; override with the REQUEST_TIMEOUT_SECONDS environment variable.
DEFAULT_TIMEOUT: float | None = None


def create_app(request_timeout: float | None = DEFAULT_TIMEOUT) -> FastAPI:
    """Build a fresh application with its own metrics registry."""
    application = FastAPI(title="fastapi-service-observability")

    @application.get("/", include_in_schema=False)
    async def read_root() -> dict[str, str]:
        return {"Hello": "World"}

    @application.get("/items/{item_id}")
    async def read_item(item_id: int) -> dict[str, int]:
        # The concrete id must never become a metric label: requests here
        # aggregate under the route template ``/items/{item_id}``.
        return {"item_id": item_id}

    @application.get("/error")
    async def raise_error() -> None:
        # Escapes the endpoint without a response; the middleware records
        # status 500 with outcome=exception, then the server renders 500.
        raise RuntimeError("intentional failure for observability tests")

    @application.get("/http-error/{code}")
    async def raise_http_error(code: int) -> None:
        # A handled HTTP error: the response exists, so it is recorded with
        # its real status and the server_error/client_error outcome.
        if code < 400 or code > 599:
            raise HTTPException(status_code=400, detail="code must be 4xx/5xx")
        raise HTTPException(status_code=code, detail=f"requested status {code}")

    @application.get("/slow")
    async def slow(delay: float = 0.0) -> dict[str, float]:
        await asyncio.sleep(max(0.0, delay))
        return {"delay": delay}

    setup_observability(application, request_timeout=request_timeout)
    return application


_timeout_env = os.environ.get("REQUEST_TIMEOUT_SECONDS")
_timeout = float(_timeout_env) if _timeout_env else None
app = create_app(request_timeout=_timeout)
