"""Metric name definitions and the shared HTTP registry factory.

Three families cover the required signal set, and they are mutually
consistent by construction (see the middleware): every request increments
``http_inflight_requests`` on entry and decrements it on exit, increments
exactly one ``http_requests_total`` series, and observes exactly one
``http_request_duration_seconds`` sample, so

    sum(http_requests_total) == sum(http_request_duration_seconds_count)

holds at every instant for completed requests, and in-flight requests are
exactly those that have not yet contributed to either.

The registry is created with a **series budget** (``max_series``): once the
number of distinct label combinations across these families reaches the
budget, unseen combinations are folded into a fixed ``__overflow__`` series
per family and counted in ``observability_overflow_total`` -- requests are
never dropped and the exposed series count never exceeds the budget.  The
registry also carries four internal ``observability_*`` self-monitoring
families (current series, configured limit, folded recordings, last render
duration) which follow the same master ``enabled`` switch as the HTTP
families.
"""
from __future__ import annotations

from .registry import MetricsRegistry

#: Total completed requests, labelled by method/route/status/outcome.
HTTP_REQUESTS_TOTAL = "http_requests_total"
#: Wall-clock handling duration per request, labelled by method/route/outcome.
HTTP_REQUEST_DURATION_SECONDS = "http_request_duration_seconds"
#: Requests currently being handled, labelled by method/route.
HTTP_INFLIGHT = "http_inflight_requests"

#: Default budget of distinct series shared by the HTTP families.  High
#: enough to never trigger for a well-formed route table, low enough to
#: stop scanner/probe traffic from growing the exposition without bound.
#: Pass ``None`` to disable the cap entirely.
DEFAULT_MAX_SERIES: int = 1000

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


def create_http_registry(
    *,
    enabled: bool = True,
    duration_enabled: bool = True,
    max_series: int | None = DEFAULT_MAX_SERIES,
) -> MetricsRegistry:
    """Create a fresh registry with the standard HTTP families registered.

    A new registry is created per application instance, so rebuilding the app
    can never produce duplicate series.

    * ``enabled=False`` disables the whole pipeline (master switch): every
      family -- including the internal ``observability_*`` self-monitoring
      families -- accepts no-op writes and is omitted from the exposition.
    * ``duration_enabled=False`` disables only the duration histogram.
    * ``max_series`` bounds the number of distinct series across the HTTP
      families; ``None`` disables the cap.  Self-monitoring families are
      always exempt from the cap.
    """
    registry = MetricsRegistry(max_series=max_series, self_monitoring=True)
    if not enabled:
        # Flip the internal families registered above; the HTTP families
        # below are registered directly with enabled=False.
        registry.set_all_enabled(False)
    registry.register_counter(
        HTTP_REQUESTS_TOTAL,
        "Total number of completed HTTP requests.",
        _REQUEST_LABELS,
        enabled=enabled,
    )
    registry.register_histogram(
        HTTP_REQUEST_DURATION_SECONDS,
        "Wall-clock duration of HTTP request handling in seconds.",
        _DURATION_LABELS,
        DURATION_BUCKETS,
        enabled=enabled and duration_enabled,
    )
    registry.register_gauge(
        HTTP_INFLIGHT,
        "Number of HTTP requests currently being handled.",
        _INFLIGHT_LABELS,
        enabled=enabled,
    )
    return registry
