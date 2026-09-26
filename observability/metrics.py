"""Metric name definitions and the shared HTTP registry factory.

Three families cover the required signal set, and they are mutually
consistent by construction (see the middleware): every request increments
``http_inflight_requests`` on entry and decrements it on exit, increments
exactly one ``http_requests_total`` series, and observes exactly one
``http_request_duration_seconds`` sample, so

    sum(http_requests_total) == sum(http_request_duration_seconds_count)

holds at every instant for completed requests, and in-flight requests are
exactly those that have not yet contributed to either.
"""
from __future__ import annotations

from .registry import MetricsRegistry

#: Total completed requests, labelled by method/route/status/outcome.
HTTP_REQUESTS_TOTAL = "http_requests_total"
#: Wall-clock handling duration per request, labelled by method/route/outcome.
HTTP_REQUEST_DURATION_SECONDS = "http_request_duration_seconds"
#: Requests currently being handled, labelled by method/route.
HTTP_INFLIGHT = "http_inflight_requests"

#: Fixed latency buckets (seconds).  Bounded set -> bounded series count.
DURATION_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

_REQUEST_LABELS = ("method", "route", "status", "outcome")
_DURATION_LABELS = ("method", "route", "outcome")
_INFLIGHT_LABELS = ("method", "route")


def create_http_registry(*, duration_enabled: bool = True) -> MetricsRegistry:
    """Create a fresh registry with the standard HTTP families registered.

    A new registry is created per application instance, so rebuilding the app
    can never produce duplicate series.  ``duration_enabled=False`` disables
    the duration histogram: it is then excluded from the exposition output
    and observations become no-ops.
    """
    registry = MetricsRegistry()
    registry.register_counter(
        HTTP_REQUESTS_TOTAL,
        "Total number of completed HTTP requests.",
        _REQUEST_LABELS,
    )
    registry.register_histogram(
        HTTP_REQUEST_DURATION_SECONDS,
        "Wall-clock duration of HTTP request handling in seconds.",
        _DURATION_LABELS,
        DURATION_BUCKETS,
        enabled=duration_enabled,
    )
    registry.register_gauge(
        HTTP_INFLIGHT,
        "Number of HTTP requests currently being handled.",
        _INFLIGHT_LABELS,
    )
    return registry
