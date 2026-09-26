"""Unit tests for the stable label vocabulary.

Each test prints the input and the rule that produced the expectation so the
judgement basis is visible in the test log (``pytest -s``).
"""
from __future__ import annotations

import pytest

from observability.labels import (
    ALL_OUTCOMES,
    OUTCOME_DISCONNECT,
    OUTCOME_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
    ROUTE_UNMATCHED,
    STATUS_NONE,
    classify_outcome,
    normalize_method,
    normalize_route,
    normalize_status,
)


def _report(case: str, given: object, rule: str, expected: object, actual: object) -> None:
    print(f"\n[{case}] input={given!r} rule={rule} expected={expected!r} actual={actual!r}")


@pytest.mark.parametrize(
    ("given", "expected"),
    [("get", "GET"), ("POST", "POST"), ("", "UNKNOWN"), (None, "UNKNOWN")],
)
def test_normalize_method(given: str | None, expected: str) -> None:
    actual = normalize_method(given)
    _report("method", given, "upper-case; empty/None -> UNKNOWN", expected, actual)
    assert actual == expected


class _FakeRoute:
    def __init__(self, path: str, path_format: str | None = None) -> None:
        self.path = path
        self.path_format = path_format if path_format is not None else path


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (_FakeRoute("/items/{item_id}"), "/items/{item_id}"),
        (_FakeRoute("/items/{item_id}", path_format="/items/{item_id}"), "/items/{item_id}"),
        (None, ROUTE_UNMATCHED),
        (object(), ROUTE_UNMATCHED),
        (_FakeRoute(""), ROUTE_UNMATCHED),
    ],
)
def test_normalize_route_bounded(given: object, expected: str) -> None:
    actual = normalize_route(given)
    _report("route", given, "template path; anything unusable -> __unmatched__", expected, actual)
    assert actual == expected


@pytest.mark.parametrize(
    ("given", "expected"),
    [(200, "200"), (404, "404"), (503, "503"), (None, STATUS_NONE)],
)
def test_normalize_status(given: int | None, expected: str) -> None:
    actual = normalize_status(given)
    _report("status", given, "3-digit code; None -> none", expected, actual)
    assert actual == expected


@pytest.mark.parametrize(
    ("status", "raised", "timed_out", "disconnected", "expected"),
    [
        (200, False, False, False, OUTCOME_OK),
        (404, False, False, False, OUTCOME_OK),
        (500, False, False, False, OUTCOME_ERROR),
        (503, False, False, False, OUTCOME_ERROR),
        (None, True, False, False, OUTCOME_EXCEPTION),
        (500, True, False, False, OUTCOME_EXCEPTION),
        (None, False, True, False, OUTCOME_TIMEOUT),
        (None, False, False, True, OUTCOME_DISCONNECT),
        # precedence checks: timeout > disconnect > exception > error > ok
        (None, True, True, True, OUTCOME_TIMEOUT),
        (None, True, False, True, OUTCOME_DISCONNECT),
        (500, True, False, False, OUTCOME_EXCEPTION),
        (None, False, False, False, OUTCOME_EXCEPTION),
    ],
)
def test_classify_outcome(
    status: int | None, raised: bool, timed_out: bool, disconnected: bool, expected: str
) -> None:
    actual = classify_outcome(status, raised=raised, timed_out=timed_out, disconnected=disconnected)
    given = dict(status=status, raised=raised, timed_out=timed_out, disconnected=disconnected)
    _report("outcome", given, "timeout>disconnect>exception>error(5xx)>ok", expected, actual)
    assert actual == expected
    assert actual in ALL_OUTCOMES
