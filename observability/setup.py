"""Idempotent wiring of observability onto a FastAPI application."""
from __future__ import annotations

from fastapi import FastAPI

from .metrics import create_http_registry
from .registry import MetricsRegistry

SETUP_ATTR = "_observability_setup"
METRICS_ROUTE_PATH = "/metrics"


def setup_observability(
    app: FastAPI,
    *,
    request_timeout: float | None = 5.0,
    duration_enabled: bool = True,
    registry: MetricsRegistry | None = None,
) -> MetricsRegistry:
    raise NotImplementedError
