"""Idempotent wiring of observability onto a FastAPI application.

``setup_observability`` may be called repeatedly on the same application
(e.g. by tests or by a factory that is invoked more than once): subsequent
calls return the existing registry without adding duplicate middleware or a
duplicate ``/metrics`` route.  Rebuilding the *application* creates a fresh
registry, so no duplicate series can ever appear.
"""
from __future__ import annotations

from fastapi import FastAPI

from .endpoint import build_metrics_router
from .metrics import HTTP_REQUEST_DURATION_SECONDS, create_http_registry
from .middleware import MetricsASGIMiddleware
from .registry import MetricsRegistry

#: Attribute used to memoize the setup state on the app instance.
SETUP_ATTR = "_observability_setup"


def setup_observability(
    app: FastAPI,
    *,
    request_timeout: float | None = 5.0,
    duration_enabled: bool = True,
    registry: MetricsRegistry | None = None,
) -> MetricsRegistry:
    """Attach metrics middleware and the ``/metrics`` endpoint to ``app``.

    Returns the registry in use (the existing one on repeated calls).
    """
    existing = getattr(app, SETUP_ATTR, None)
    if existing is not None:
        return existing

    if registry is None:
        registry = create_http_registry(duration_enabled=duration_enabled)
    else:
        registry.set_enabled(HTTP_REQUEST_DURATION_SECONDS, duration_enabled)

    app.include_router(build_metrics_router(registry))
    app.add_middleware(
        MetricsASGIMiddleware,
        registry=registry,
        router=app,
        request_timeout=request_timeout,
    )

    setattr(app, SETUP_ATTR, registry)
    return registry


def get_registry(app: FastAPI) -> MetricsRegistry | None:
    """Return the registry attached by :func:`setup_observability`, if any."""
    return getattr(app, SETUP_ATTR, None)
