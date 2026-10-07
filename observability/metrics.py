"""Metric name definitions and the shared HTTP registry factory.

Three user-facing families cover the required signal set, and they are
mutually consistent by construction (see the middleware): every request
increments ``http_inflight_requests`` on entry and decrements it on exit,
increments exactly one ``http_requests_total`` series, and observes exactly
one ``http_request_duration_seconds`` sample, so

    sum(http_requests_total) == sum(http_request_duration_seconds_count)

holds at every instant for completed requests, and in-flight requests are
exactly those that have not yet contributed to either.

Cardinality safety
------------------
The counter and histogram families are registered with a per-family
``max_series`` cap (default :data:`DEFAULT_MAX_SERIES`).  Once the natural
series budget of such a family is exhausted, further combinations converge
to a single fallback series (all label positions ``__overflow__``); five
bounded self-monitoring families (``observability_*``) expose the cap,
current series counts, overflow state/counts and render cost.  The in-flight
gauge is intentionally left uncapped: its key space is naturally bounded and
its paired +1/-1 writes must always resolve to the same key.
``max_series=None`` disables the cap; the whole pipeline (self-monitoring
included) follows ``enabled``.
"""
from __future__ import annotations

from .registry import MetricsRegistry

#: Total completed requests, labelled by method/route/status/outcome/result.
HTTP_REQUESTS_TOTAL = "http_requests_total"
#: Wall-clock handling duration per request, labelled by method/route/outcome.
HTTP_REQUEST_DURATION_SECONDS = "http_request_duration_seconds"
#: Requests currently being handled, labelled by method/route.
HTTP_INFLIGHT = "http_inflight_requests"

# --- self-monitoring families -----------------------------------------------
#: Current exposed series count per user family (gauge, label ``family``).
OBS_SERIES = "observability_metric_series"
#: Configured per-family cap (gauge, label ``family``; 0 == unlimited).
OBS_SERIES_CAP = "observability_metric_series_cap"
#: 1 while the family is currently redirecting to the fallback (gauge).
OBS_OVERFLOW_ACTIVE = "observability_metric_overflow_active"
#: Total writes redirected into the fallback since start (counter).
OBS_OVERFLOW_TOTAL = "observability_metric_overflow_total"
#: Wall-clock duration of one scrape render (histogram, no labels).
OBS_RENDER_SECONDS = "observability_render_duration_seconds"

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

#: Bounded buckets for the self-monitoring render-duration histogram.
RENDER_DURATION_BUCKETS: tuple[float, ...] = (
    0.0001,
    0.0005,
    0.001,
    0.005,
    0.01,
    0.05,
    0.1,
    0.5,
    1.0,
)

#: Default per-family series cap (including the reserved fallback series).
#: Generous enough for any realistic route/method/status/outcome vocabulary
#: while keeping a single scrape response strictly bounded on public traffic.
DEFAULT_MAX_SERIES: int = 500

_REQUEST_LABELS = ("method", "route", "status", "outcome", "result")
_DURATION_LABELS = ("method", "route", "outcome")
_INFLIGHT_LABELS = ("method", "route")


def create_http_registry(
    *,
    duration_enabled: bool = True,
    max_series: int | None = DEFAULT_MAX_SERIES,
    enabled: bool = True,
) -> MetricsRegistry:
    """Create a fresh registry with the standard HTTP families registered.

    A new registry is created per application instance, so rebuilding the app
    can never produce duplicate series.  ``duration_enabled=False`` disables
    the duration histogram: it is then excluded from the exposition output
    and observations become no-ops.  ``max_series`` sets the per-family
    cardinality cap (``None`` = unlimited; see
    :meth:`MetricsRegistry._admit`).  ``enabled=False`` starts the registry
    with the global switch off -- no families, self-monitoring included, are
    exposed until it is switched back on.
    """
    registry = MetricsRegistry()
    registry.register_counter(
        HTTP_REQUESTS_TOTAL,
        "Total number of completed HTTP requests.",
        _REQUEST_LABELS,
        max_series=max_series,
    )
    registry.register_histogram(
        HTTP_REQUEST_DURATION_SECONDS,
        "Wall-clock duration of HTTP request handling in seconds.",
        _DURATION_LABELS,
        DURATION_BUCKETS,
        enabled=duration_enabled,
        max_series=max_series,
    )
    registry.register_gauge(
        HTTP_INFLIGHT,
        "Number of HTTP requests currently being handled.",
        _INFLIGHT_LABELS,
        # The in-flight gauge is deliberately left uncapped: its vocabulary
        # (route template x bounded method set) is naturally finite, and more
        # importantly its +1/-1 pair for one request must always land on the
        # same key.  With a cap, the decrement could resolve differently from
        # the increment (e.g. an up-front probe key versus a post-hoc route),
        # which would strand value in the fallback series.
        max_series=None,
    )
    registry.register_self_monitoring(
        series_name=OBS_SERIES,
        cap_name=OBS_SERIES_CAP,
        overflow_active_name=OBS_OVERFLOW_ACTIVE,
        overflow_total_name=OBS_OVERFLOW_TOTAL,
        render_duration_name=OBS_RENDER_SECONDS,
        render_buckets=RENDER_DURATION_BUCKETS,
    )
    if not enabled:
        registry.set_pipeline_enabled(False)
    return registry
