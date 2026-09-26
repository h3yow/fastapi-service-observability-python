"""Stable label vocabulary for HTTP observability.

All label values are constants from this module so that metric series can
never grow without bound (unknown routes collapse to a single value).
"""
from __future__ import annotations

from typing import Final

ROUTE_UNMATCHED: Final[str] = "__unmatched__"
ROUTE_METRICS: Final[str] = "/metrics"

OUTCOME_OK: Final[str] = "ok"
OUTCOME_ERROR: Final[str] = "error"
OUTCOME_EXCEPTION: Final[str] = "exception"
OUTCOME_TIMEOUT: Final[str] = "timeout"
OUTCOME_DISCONNECT: Final[str] = "client_disconnect"

STATUS_NONE: Final[str] = "none"

ALL_OUTCOMES: Final[tuple[str, ...]] = (
    OUTCOME_OK,
    OUTCOME_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_TIMEOUT,
    OUTCOME_DISCONNECT,
)


def normalize_method(method: str | None) -> str:
    raise NotImplementedError


def normalize_route(route: object) -> str:
    raise NotImplementedError


def normalize_status(status: int | None) -> str:
    raise NotImplementedError


def classify_outcome(status: int | None, *, raised: bool, timed_out: bool, disconnected: bool) -> str:
    raise NotImplementedError
