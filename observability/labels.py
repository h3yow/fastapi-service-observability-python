"""Stable label vocabulary for HTTP observability.

All label values used by the metrics pipeline come from this module so that
metric series can never grow without bound:

* ``route`` is the *route template* (e.g. ``/items/{item_id}``) taken from the
  matched route, never the concrete request path.  Requests that match no
  route (404) collapse to the single value ``__unmatched__``; a 405 keeps the
  template of the path it matched.
* ``method`` is the upper-cased HTTP method (a small, protocol-bounded set).
* ``status`` is the 3-digit response code for codes in the standard range
  100--599, ``other`` for any non-standard code an upstream or proxy may
  invent, or ``none`` when the connection ended before any response could be
  produced.
* ``outcome`` is one of a fixed set of six terminal states (see
  :data:`ALL_OUTCOMES`) so that client errors, server errors, timeouts and
  client disconnects are distinguishable from each other and from normal
  responses.
"""
from __future__ import annotations

from typing import Final

#: Route label used for requests that matched no route (404/405).
ROUTE_UNMATCHED: Final[str] = "__unmatched__"

#: Route template of the metrics exposition endpoint itself.
ROUTE_METRICS: Final[str] = "/metrics"

# --- outcome label values ---------------------------------------------------
#: Response fully sent with status < 400.
OUTCOME_OK: Final[str] = "ok"
#: Response fully sent with a 4xx status (the client sent a bad request).
OUTCOME_CLIENT_ERROR: Final[str] = "client_error"
#: Response fully sent with status >= 500 (no exception escaped the app).
OUTCOME_ERROR: Final[str] = "error"
#: An exception escaped the application while handling the request.
OUTCOME_EXCEPTION: Final[str] = "exception"
#: The request exceeded the configured request timeout.
OUTCOME_TIMEOUT: Final[str] = "timeout"
#: The client went away before the response completed.
OUTCOME_DISCONNECT: Final[str] = "client_disconnect"

#: Status label used when no response status was ever produced.
STATUS_NONE: Final[str] = "none"
#: Status label for codes outside the standard 100--599 range.
STATUS_OTHER: Final[str] = "other"

ALL_OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_OK,
    OUTCOME_CLIENT_ERROR,
    OUTCOME_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_TIMEOUT,
    OUTCOME_DISCONNECT,
)


def normalize_method(method: str | None) -> str:
    """Return a bounded, upper-cased method label."""
    if not method:
        return "UNKNOWN"
    return method.upper()


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
    """Return the status label: ``"200"`` ... ``"599"``, ``"other"`` or ``"none"``.

    Codes inside the standard 100--599 range keep their exact 3-digit form
    (a protocol-bounded vocabulary).  Anything outside that range -- e.g.
    non-standard codes invented by upstreams or proxies -- collapses to the
    single value ``other`` so exotic statuses can never grow the label space.
    """
    if status is None:
        return STATUS_NONE
    code = int(status)
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
    4. ``error``             -- a response with status >= 500 was fully sent
    5. ``client_error``      -- a 4xx response was fully sent
    6. ``ok``                -- anything else that produced a response

    Non-standard codes follow the same numeric ranges (e.g. a proxy's 777
    lands in ``error``), so the six outcomes always add up to the total
    number of completed requests.
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
    if status >= 500:
        return OUTCOME_ERROR
    if status >= 400:
        return OUTCOME_CLIENT_ERROR
    return OUTCOME_OK
