"""Prometheus exposition endpoint.

Serves the registry in the standard text exposition format
(``text/plain; version=0.0.4``) so any Prometheus-compatible scraper can
consume it.  Disabled metric families are already excluded by
:meth:`MetricsRegistry.render`.
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from .labels import ROUTE_METRICS
from .registry import MetricsRegistry

#: Content type required by the Prometheus text exposition format.
EXPOSITION_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def render_metrics(registry: MetricsRegistry) -> PlainTextResponse:
    return PlainTextResponse(registry.render(), media_type=EXPOSITION_CONTENT_TYPE)


def build_metrics_router(registry: MetricsRegistry) -> APIRouter:
    """Build a router exposing ``GET /metrics`` for ``registry``."""
    router = APIRouter()

    @router.get(ROUTE_METRICS, include_in_schema=False)
    async def metrics_endpoint() -> PlainTextResponse:
        return await render_metrics(registry)

    return router
