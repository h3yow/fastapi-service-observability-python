"""Stable label vocabulary for HTTP observability.

All label values used by the metrics pipeline come from this module so that
metric series can never grow without bound:

* ``route`` is the *route template* (e.g. ``/items/{item_id}``) taken from the
  matched route, never the concrete request path.  Requests that match no
  route (404) collapse to the single value ``__unmatched__``; a 405 keeps the
  template of the path it matched.
* ``method`` is one of the nine standard HTTP verbs (upper-cased), ``UNKNOWN``
  when missing, or ``OTHER`` for any non-standard verb a scanner may send.
* ``status`` is the 3-digit response code when it lies in the standard HTTP
  range 100-599, ``none`` when no response was produced, or ``other`` for any
  out-of-range/non-numeric code an upstream might emit.
* ``outcome`` is one of a fixed set of five terminal states (see
  :data:`ALL_OUTCOMES`) so that errors, timeouts and client disconnects are
  distinguishable from each other and from normal responses.
* ``result`` is one of a fixed set of six dashboard-ready classes (see
  :data:`ALL_RESULTS`): it splits the old ``ok`` bucket into successful and
  client-error responses so error-rate/availability boards need no heuristics.
* ``__overflow__`` is the single reserved value used for every label position
  of the cardinality-cap fallback series (see :mod:`observability.registry`).
"""
from __future__ import annotations

from typing import Final

#: Route label used for requests that matched no route (404/405).
ROUTE_UNMATCHED: Final[str] = "__unmatched__"

#: Route template of the metrics exposition endpoint itself.
ROUTE_METRICS: Final[str] = "/metrics"

#: Value used in *every* label position of a cardinality-cap fallback series.
LABEL_OVERFLOW: Final[str] = "__overflow__"

# --- method label values -----------------------------------------------------
#: The nine verbs defined by the HTTP semantics RFC; anything else is bounded
#: to ``OTHER`` so crafted methods cannot open new series.
STANDARD_METHODS: Final[frozenset[str]] = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "CONNECT", "OPTIONS", "TRACE", "PATCH"}
)
METHOD_UNKNOWN: Final[str] = "UNKNOWN"
METHOD_OTHER: Final[str] = "OTHER"

# --- status label values -----------------------------------------------------
#: Status label used when no response status was ever produced.
STATUS_NONE: Final[str] = "none"
#: Status label used for out-of-range / non-numeric status codes, so an
#: upstream emitting e.g. 612 or 999 cannot open one series per code.
STATUS_OTHER: Final[str] = "other"

# --- outcome label values ---------------------------------------------------
#: Response fully sent with status < 500.
OUTCOME_OK: Final[str] = "ok"
#: Response fully sent with status >= 500 (no exception escaped the app).
OUTCOME_ERROR: Final[str] = "error"
#: An exception escaped the application while handling the request.
OUTCOME_EXCEPTION: Final[str] = "exception"
#: The request exceeded the configured request timeout.
OUTCOME_TIMEOUT: Final[str] = "timeout"
#: The client went away before the response completed.
OUTCOME_DISCONNECT: Final[str] = "client_disconnect"

ALL_OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_OK,
    OUTCOME_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_TIMEOUT,
    OUTCOME_DISCONNECT,
)

# --- result label values (dashboard-ready failure classification) -----------
#: A complete response with a 1xx/2xx/3xx status.
RESULT_SUCCESS: Final[str] = "success"
#: A complete response with a 4xx status (client-side mistake).
RESULT_CLIENT_ERROR: Final[str] = "client_error"
#: A complete response with a 5xx (or non-standard) status, no exception.
RESULT_SERVER_ERROR: Final[str] = "server_error"
#: An exception escaped the application while handling the request.
RESULT_EXCEPTION: Final[str] = "exception"
#: The request exceeded the configured request timeout.
RESULT_TIMEOUT: Final[str] = "timeout"
#: The client went away before the response completed.
RESULT_CLIENT_DISCONNECT: Final[str] = "client_disconnect"

ALL_RESULTS: Final[tuple[str, ...]] = (
    RESULT_SUCCESS,
    RESULT_CLIENT_ERROR,
    RESULT_SERVER_ERROR,
    RESULT_EXCEPTION,
    RESULT_TIMEOUT,
    RESULT_CLIENT_DISCONNECT,
)


def normalize_method(method: str | None) -> str:
    """Return a bounded method label.

    Missing/empty -> ``UNKNOWN``; the nine standard verbs are kept verbatim
    (upper-cased); any other token a scanner may invent collapses to
    ``OTHER`` so the method vocabulary stays finite.
    """
    if not method:
        return METHOD_UNKNOWN
    upper = method.upper()
    return upper if upper in STANDARD_METHODS else METHOD_OTHER


def normalize_route(route: object) -> str:
    """Return the route label for a resolved route template.

    ``route`` is expected to be the ``route`` entry that Starlette puts into
    the ASGI scope (a route object with a ``path``/``path_format`` attribute)
    or ``None`` when nothing matched.  Anything that is not a usable template
    collapses to :data:`ROUTE_UNMATCHED`, which keeps the label cardinality
    bounded no matter how many distinct URLs are requested.
    """
    template: object = None
    if route is not None:
        template = getattr(route, "path_format", None) or getattr(route, "path", None)
    if isinstance(template, str) and template:
        return template
    return ROUTE_UNMATCHED


def normalize_status(status: int | None) -> str:
    """Return the bounded status label.

    * ``None`` (no response ever produced) -> ``none``.
    * Integers in the standard HTTP range 100-599 -> the 3-digit string
      (``"200"`` ... ``"599"``); this covers 1xx/2xx/3xx/4xx/5xx.
    * Anything else (out-of-range integer or a non-numeric code an upstream
      might emit) collapses to ``other`` so arbitrary codes cannot open one
      series per value.
    """
    if status is None:
        return STATUS_NONE
    try:
        code = int(status)
    except (TypeError, ValueError):
        return STATUS_OTHER
    if 100 <= code <= 599:
        return str(code)
    return STATUS_OTHER


def classify_outcome(
    status: int | None,
    *,
    raised: bool,
    timed_out: bool,
    disconnected: bool,
) -> str:
    """Map the terminal facts of a request to exactly one outcome label.

    Precedence is fixed and documented so that every request lands in exactly
    one bucket even when several abnormal conditions coincide:

    1. ``timeout``           -- the deadline fired (strongest signal of slowness)
    2. ``client_disconnect`` -- the client went away mid-request
    3. ``exception``         -- an exception escaped the application
    4. ``error``             -- a 5xx response, or a non-standard code outside
                                the HTTP range 100-599, was fully sent
    5. ``ok``                -- anything else that produced a response
    """
    if timed_out:
        return OUTCOME_TIMEOUT
    if disconnected:
        return OUTCOME_DISCONNECT
    if raised:
        return OUTCOME_EXCEPTION
    if status is None:
        # The app returned without producing a response and without any other
        # recorded cause: treat it as an abnormal termination so conservation
        # still holds and the case stays visible.
        return OUTCOME_EXCEPTION
    if status >= 500 or status < 100 or status > 599:
        return OUTCOME_ERROR
    return OUTCOME_OK


def classify_result(
    status: int | None,
    *,
    raised: bool,
    timed_out: bool,
    disconnected: bool,
) -> str:
    """Map the terminal facts of a request to exactly one ``result`` class.

    This is the dashboard-facing companion of :func:`classify_outcome`: the
    failure *mode* flags (``timeout`` / ``client_disconnect`` / ``exception``)
    keep the exact same precedence, while complete responses are split by
    status range:

    1. ``timeout``           -- the deadline fired
    2. ``client_disconnect`` -- the client went away mid-request
    3. ``exception``         -- an exception escaped the application
    4. ``server_error``      -- a complete 5xx response (no exception)
    5. ``client_error``      -- a complete 4xx response
    6. ``success``           -- a complete 1xx/2xx/3xx response

    A completed response whose code lies outside the standard HTTP range
    (see :func:`normalize_status`, exposed as ``status="other"``) is treated
    as a server-side failure: an unexpected code is not something a client
    can fix, so it belongs in the availability/error-rate numerator.
    Requests that produced no response and no other cause classify as
    ``exception`` exactly as they do for ``outcome``.
    """
    if timed_out:
        return RESULT_TIMEOUT
    if disconnected:
        return RESULT_CLIENT_DISCONNECT
    if raised or status is None:
        return RESULT_EXCEPTION
    if status < 100 or status > 599:
        return RESULT_SERVER_ERROR
    if status >= 500:
        return RESULT_SERVER_ERROR
    if status >= 400:
        return RESULT_CLIENT_ERROR
    return RESULT_SUCCESS
