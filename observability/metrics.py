"""Metric name definitions and the shared HTTP registry factory."""
from __future__ import annotations

from .registry import MetricsRegistry

HTTP_REQUESTS_TOTAL = "http_requests_total"
HTTP_REQUEST_DURATION_SECONDS = "http_request_duration_seconds"
HTTP_INFLIGHT = "http_inflight_requests"


def create_http_registry(*, duration_enabled: bool = True) -> MetricsRegistry:
    raise NotImplementedError
