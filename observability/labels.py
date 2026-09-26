"""Stable label vocabulary for HTTP observability.

All label values used by the metrics pipeline come from this module so that
metric series can never grow without bound:

* ``route`` is the *route template* (e.g. ``/items/{item_id}``) taken from the
  matched route, never the concrete request path.  Requests that match no
  route (404) collapse to the single value ``__unmatched__``; a 405 keeps the
  template of the path it matched.
* ``method`` is the upper-cased HTTP method (a small, protocol-bounded set).
* ``status`` is the 3-digit response code, or ``none`` when the connection
  ended before any response could be produced.
* ``outcome`` is one of a fixed set of five terminal states (see
  :data:`ALL_OUTCOMES`) so that errors, timeouts and client disconnects are
  distinguishable from each other and from normal responses.
"""
from __future__ import annotations

from typing import Final

#: Route label used for requests that matched no route (404/405).
ROUTE_UNMATCHED: Final[str] = "__unmatched__"

#: Route template of the metrics exposition endpoint itself.
ROUTE_METRICS: Final[str] = "/metrics"

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

#: Status label used when no response status was ever produced.
STATUS_NONE: Final[str] = "none"

ALL_OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_OK,
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
    """Return the status label (``"200"`` ... ``"599"`` or ``"none"``)."""
    if status is None:
        return STATUS_NONE
    return str(int(status))


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
    4. ``error``             -- a 5xx response was fully sent
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
    if status >= 500:
        return OUTCOME_ERROR
    return OUTCOME_OK
