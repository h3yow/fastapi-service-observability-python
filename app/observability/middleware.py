"""ASGI middleware that records per-request HTTP metrics.

Design properties
-----------------
* Requests are attributed to the matched route *template* (e.g.
  ``/items/{item_id}``), never to the concrete path. Unmatched requests and
  requests with unknown HTTP methods share single sentinel label values, so
  label cardinality stays bounded under arbitrary traffic.
* Each request contributes exactly one counter increment, exactly one
  histogram observation and a balanced (+1/-1) gauge change: the three
  metrics stay mutually consistent.
* All storage is pre-allocated and every update is an O(1) dictionary lookup
  behind a short-lived lock, so collection does not slow down with history.
* Failure kinds are distinguishable through the ``outcome`` label:
  ``success`` / ``client_error`` / ``server_error`` / ``exception`` /
  ``client_disconnect`` / ``timeout``.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from .metrics import (
    DEFAULT_BUCKETS,
    Counter,
    Gauge,
    Histogram,
    Registry,
)

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]
App = Callable[[Scope, Receive, Send], Awaitable[None]]

logger = logging.getLogger("app.observability")

# ---------------------------------------------------------------------------
# Canonical, bounded label vocabularies
# ---------------------------------------------------------------------------

ROUTE_UNMATCHED = "unmatched"
METHOD_OTHER = "OTHER"
METHODS: tuple[str, ...] = (
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS",
    "CONNECT", "TRACE", METHOD_OTHER,
)

# Status codes that are emitted verbatim. Any other numeric status collapses
# to its class bucket (1xx ... 5xx); non-HTTP results collapse to "0xx".
LITERAL_STATUSES: frozenset[str] = frozenset(
    {
        "100",
        "200", "201", "202", "204",
        "301", "302", "304",
        "400", "401", "403", "404", "405", "409", "418", "422", "429",
        "499",  # nginx convention: client closed the connection
        "500", "501", "502", "503", "504",
    }
)
STATUS_CLASS_BUCKETS: tuple[str, ...] = ("1xx", "2xx", "3xx", "4xx", "5xx", "0xx")

OUTCOME_SUCCESS = "success"
OUTCOME_CLIENT_ERROR = "client_error"
OUTCOME_SERVER_ERROR = "server_error"
OUTCOME_EXCEPTION = "exception"
OUTCOME_CLIENT_DISCONNECT = "client_disconnect"
OUTCOME_TIMEOUT = "timeout"
OUTCOMES: tuple[str, ...] = (
    OUTCOME_SUCCESS,
    OUTCOME_CLIENT_ERROR,
    OUTCOME_SERVER_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_CLIENT_DISCONNECT,
    OUTCOME_TIMEOUT,
)

_STATUS_DISCONNECT = "499"
_STATUS_TIMEOUT = "504"
_STATUS_EXCEPTION = "500"


def canonical_method(method: str | None) -> str:
    """Normalize an HTTP method to the closed vocabulary."""
    upper = (method or "").upper()
    return upper if upper in METHODS[:-1] else METHOD_OTHER


def canonical_status(code: int | None) -> str:
    """Normalize an HTTP status code to a pre-registered label value."""
    text = str(code)
    if text in LITERAL_STATUSES:
        return text
    if code is not None and 100 <= code <= 599:
        return f"{code // 100}xx"
    return "0xx"


def outcome_for_status(status: str) -> str:
    """Map a canonical status label to its request-class outcome."""
    if status in ("1xx", "2xx", "3xx") or status[0] in "123":
        return OUTCOME_SUCCESS
    if status == "4xx" or status[0] == "4":
        return OUTCOME_CLIENT_ERROR
    return OUTCOME_SERVER_ERROR


def request_label_pairs() -> tuple[tuple[str, str], ...]:
    """All pre-registered (status, outcome) combinations."""
    pairs: set[tuple[str, str]] = set()
    # Normal responses: literal codes and class buckets.
    for status in LITERAL_STATUSES:
        pairs.add((status, outcome_for_status(status)))
    for bucket in STATUS_CLASS_BUCKETS:
        pairs.add((bucket, outcome_for_status(bucket)))
    # An exception can escape after response streaming started, in which
    # case the headers already carried an arbitrary (but known) status.
    for status in LITERAL_STATUSES:
        pairs.add((status, OUTCOME_EXCEPTION))
    for bucket in STATUS_CLASS_BUCKETS:
        pairs.add((bucket, OUTCOME_EXCEPTION))
    # The dedicated failure channels.
    pairs.add((_STATUS_DISCONNECT, OUTCOME_CLIENT_DISCONNECT))
    pairs.add((_STATUS_TIMEOUT, OUTCOME_TIMEOUT))
    pairs.add((_STATUS_EXCEPTION, OUTCOME_EXCEPTION))
    return tuple(sorted(pairs))


def extract_route_template(scope: Scope) -> str | None:
    """Return the matched route template if the router matched one."""
    route = scope.get("route")
    if route is not None:
        template = getattr(route, "path_format", None) or getattr(
            route, "path", None
        )
        if template:
            return str(template)
    return None


# ---------------------------------------------------------------------------
# Configuration and middleware
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservabilitySettings:
    """Runtime configuration and metric handles for the middleware."""

    registry: Registry
    routes: tuple[str, ...]
    path_matches: tuple[tuple[object, str], ...]
    request_timeout: float | None
    metrics_path: str
    requests_total: Counter | None
    request_duration_seconds: Histogram | None
    requests_in_progress: Gauge | None


# Exceptions raised by servers when the peer has gone away mid-exchange.
_DISCONNECT_EXC_TYPES: tuple[type[BaseException], ...] = (
    ConnectionError,
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
)
try:  # Starlette raises this when a body read sees http.disconnect
    from starlette.requests import ClientDisconnect as _ClientDisconnect

    _DISCONNECT_EXC_TYPES = (*_DISCONNECT_EXC_TYPES, _ClientDisconnect)
except ImportError:  # pragma: no cover - Starlette is a hard dependency
    pass


class ObservabilityMiddleware:
    """Pure-ASGI middleware: counts, timings and in-flight gauge per request."""

    def __init__(self, app: App, settings: ObservabilitySettings) -> None:
        self.app = app
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # The scrape endpoint is excluded from the business HTTP metrics.
        # Otherwise a scrape would observe itself as one in-flight request
        # forever (it runs while its own snapshot is rendered), which is
        # uninformative self-reference rather than user traffic.
        if (
            scope.get("method") == "GET"
            and scope.get("path") == self._settings.metrics_path
        ):
            await self.app(scope, receive, send)
            return

        method = canonical_method(scope.get("method"))
        gauge = self._settings.requests_in_progress
        counter = self._settings.requests_total
        histogram = self._settings.request_duration_seconds

        # Pre-resolve the route before any endpoint work so the in-flight
        # gauge (observable at any instant during a long request) already
        # carries the correct template, instead of briefly living on
        # "unmatched" until http.response.start. Matching uses the same
        # pre-compiled regexes Starlette used at setup time; the router's
        # own scope["route"] remains the authoritative value checked later.
        pre_resolved = self._resolve_route(scope)
        current_route = pre_resolved or ROUTE_UNMATCHED
        if gauge is not None:
            gauge.inc((method, current_route))

        started = False
        started_status: int | None = None
        disconnect_seen = anyio.Event()

        async def receive_wrapper() -> dict:
            message = await receive()
            if message.get("type") == "http.disconnect":
                disconnect_seen.set()
            return message

        async def send_wrapper(message: dict) -> None:
            nonlocal started, started_status, current_route
            if message["type"] == "http.response.start" and not started:
                started = True
                started_status = int(message["status"])
                resolved = self._resolve_route(scope)
                if resolved is not None and resolved != current_route:
                    if gauge is not None:
                        gauge.dec((method, current_route))
                        gauge.inc((method, resolved))
                    current_route = resolved
            await send(message)

        status_label = _STATUS_EXCEPTION
        outcome = OUTCOME_EXCEPTION
        timed_out = False
        escaped_exception: BaseException | None = None
        start = time.perf_counter()

        try:
            timeout = self._settings.request_timeout
            if timeout is None:
                await self.app(scope, receive_wrapper, send_wrapper)
            else:
                with anyio.fail_after(timeout):
                    await self.app(scope, receive_wrapper, send_wrapper)
        except TimeoutError:
            timed_out = True
        except _DISCONNECT_EXC_TYPES:
            disconnect_seen.set()
        except BaseException as exc:  # noqa: BLE001 - recorded, then re-raised
            # Any other application error (including CancelledError): it is
            # classified below as "exception" and re-raised after bookkeeping
            # so the server/outer middleware handles it normally.
            escaped_exception = exc
        finally:
            # The router records the match in the scope before invoking the
            # endpoint, so an exception path (no http.response.start yet)
            # still carries its template; resolve it here so failures are
            # attributed to their route instead of the "unmatched" bucket.
            resolved = self._resolve_route(scope)
            if resolved is not None and resolved != current_route:
                if gauge is not None:
                    gauge.dec((method, current_route))
                    gauge.inc((method, resolved))
                current_route = resolved
            try:
                status_label, outcome = self._classify(
                    started=started,
                    started_status=started_status,
                    disconnect_seen=disconnect_seen.is_set(),
                    timed_out=timed_out,
                    exc_escaped=escaped_exception is not None,
                )
                duration = max(0.0, time.perf_counter() - start)
                if counter is not None:
                    counter.inc((method, current_route, status_label, outcome))
                if histogram is not None:
                    histogram.observe(
                        (method, current_route, status_label, outcome), duration
                    )
            finally:
                if gauge is not None:
                    gauge.dec((method, current_route))

        # Cancellation (server shutdown, outer task-group cancellation) must
        # always propagate, whether or not streaming had started; swallowing
        # it would strand tasks. It has already been recorded above.
        if isinstance(escaped_exception, (SystemExit, KeyboardInterrupt)):
            raise escaped_exception
        if isinstance(
            escaped_exception, anyio.get_cancelled_exc_class()
        ):
            raise escaped_exception

        if timed_out and not started:
            # The handler produced nothing: answer with a stable 504 so the
            # client sees the same status that the metrics report.
            response = PlainTextResponse(
                "Gateway Timeout\n", status_code=504
            )
            try:
                await response(scope, receive_wrapper, send)
            except Exception as timeout_send_error:  # noqa: BLE001
                # The peer may already be gone; the timeout was already
                # recorded, so failing to also deliver the 504 is benign.
                logger.debug(
                    "could not deliver synthetic 504 after timeout: %r",
                    timeout_send_error,
                )
        elif (
            escaped_exception is not None
            and not disconnect_seen.is_set()
            and not started
        ):
            # An exception escaped before any response was started; re-raise
            # so the surrounding server middleware can render its 500 page.
            raise escaped_exception

    def _resolve_route(self, scope: Scope) -> str | None:
        template = extract_route_template(scope)
        if template is not None:
            return template
        # Fallback for responses the router itself produces (notably 405),
        # where scope["route"] is not set: match the path method-agnostically.
        path = scope.get("path", "")
        for path_regex, candidate in self._settings.path_matches:
            if path_regex.match(path):  # type: ignore[union-attr]
                return candidate
        return None

    @staticmethod
    def _classify(
        *,
        started: bool,
        started_status: int | None,
        disconnect_seen: bool,
        timed_out: bool,
        exc_escaped: bool,
    ) -> tuple[str, str]:
        if disconnect_seen:
            return _STATUS_DISCONNECT, OUTCOME_CLIENT_DISCONNECT
        if timed_out:
            return _STATUS_TIMEOUT, OUTCOME_TIMEOUT
        if started and started_status is not None:
            status = canonical_status(started_status)
            if exc_escaped:
                # The headers already carried a status, but processing then
                # failed while streaming the body: keep the visible status
                # and flag the failure explicitly via the outcome label.
                return status, OUTCOME_EXCEPTION
            return status, outcome_for_status(status)
        return _STATUS_EXCEPTION, OUTCOME_EXCEPTION


# ---------------------------------------------------------------------------
# Setup
# -----------------------------------------------------------------------


_SETUP_ATTR = "observability_settings"


def _collect_route_templates(app: Starlette) -> tuple[str, ...]:
    templates: set[str] = {ROUTE_UNMATCHED}
    for route in app.routes:
        template = getattr(route, "path_format", None) or getattr(
            route, "path", None
        )
        if template:
            templates.add(str(template))
    return tuple(sorted(templates))


def _collect_path_matches(app: Starlette) -> tuple[tuple[object, str], ...]:
    matches: list[tuple[object, str]] = []
    for route in app.routes:
        path_regex = getattr(route, "path_regex", None)
        template = getattr(route, "path_format", None) or getattr(
            route, "path", None
        )
        if path_regex is not None and template:
            matches.append((path_regex, str(template)))
    return tuple(matches)


def setup_observability(
    app: Starlette,
    *,
    registry: Registry | None = None,
    request_timeout: float | None = None,
    metrics_path: str = "/metrics",
    enable_requests_total: bool = True,
    enable_request_duration_seconds: bool = True,
    enable_requests_in_progress: bool = True,
) -> ObservabilitySettings:
    """Create the HTTP metrics, mount the middleware and expose the endpoint.

    Idempotent per application: a second call returns the existing settings
    and never adds a second middleware, a second route or a second series
    set. Each app owns its own registry, so rebuilding the application yields
    a fresh, empty registry instead of duplicate registrations.
    """
    existing = getattr(app.state, _SETUP_ATTR, None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]

    registry = registry if registry is not None else Registry()

    async def metrics_endpoint(request: Request) -> PlainTextResponse:
        return PlainTextResponse(
            registry.render(), media_type=registry.content_type
        )

    # Expose the scrape endpoint first so its template is part of the set.
    if not any(
        getattr(route, "path", None) == metrics_path for route in app.routes
    ):
        app.add_route(
            metrics_path,
            metrics_endpoint,
            methods=["GET"],
            include_in_schema=False,
        )

    routes = _collect_route_templates(app)
    pairs = request_label_pairs()

    requests_total: Counter | None = None
    request_duration_seconds: Histogram | None = None
    requests_in_progress: Gauge | None = None

    if enable_requests_total:
        requests_total = Counter(
            "http_requests_total",
            "Total number of HTTP requests handled, by route template, "
            "status label and outcome.",
            ("method", "route", "status", "outcome"),
            [
                (method, route, status, outcome)
                for method in METHODS
                for route in routes
                for status, outcome in pairs
            ],
        )
        registry.register(requests_total)

    if enable_request_duration_seconds:
        request_duration_seconds = Histogram(
            "http_request_duration_seconds",
            "HTTP request handling duration in seconds, by route template, "
            "status label and outcome.",
            ("method", "route", "status", "outcome"),
            [
                (method, route, status, outcome)
                for method in METHODS
                for route in routes
                for status, outcome in pairs
            ],
            buckets=DEFAULT_BUCKETS,
        )
        registry.register(request_duration_seconds)

    if enable_requests_in_progress:
        requests_in_progress = Gauge(
            "http_requests_in_progress",
            "In-flight HTTP requests, by route template.",
            ("method", "route"),
            [(method, route) for method in METHODS for route in routes],
        )
        registry.register(requests_in_progress)

    settings = ObservabilitySettings(
        registry=registry,
        routes=routes,
        path_matches=_collect_path_matches(app),
        request_timeout=request_timeout,
        metrics_path=metrics_path,
        requests_total=requests_total,
        request_duration_seconds=request_duration_seconds,
        requests_in_progress=requests_in_progress,
    )

    app.add_middleware(ObservabilityMiddleware, settings=settings)
    setattr(app.state, _SETUP_ATTR, settings)
    return settings
