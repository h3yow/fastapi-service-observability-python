"""Unit tests for the stable label vocabulary.

Each test prints the input and the rule that produced the expectation so the
judgement basis is visible in the test log (``pytest -s``).
"""
from __future__ import annotations

import pytest

from observability.labels import (
    ALL_OUTCOMES,
    ALL_RESULTS,
    LABEL_OVERFLOW,
    METHOD_OTHER,
    OUTCOME_DISCONNECT,
    OUTCOME_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_OK,
    OUTCOME_TIMEOUT,
    RESULT_CLIENT_DISCONNECT,
    RESULT_CLIENT_ERROR,
    RESULT_EXCEPTION,
    RESULT_SERVER_ERROR,
    RESULT_SUCCESS,
    RESULT_TIMEOUT,
    ROUTE_UNMATCHED,
    STATUS_NONE,
    STATUS_OTHER,
    classify_outcome,
    classify_result,
    normalize_method,
    normalize_route,
    normalize_status,
)


def _report(case: str, given: object, rule: str, expected: object, actual: object) -> None:
    print(f"\n[{case}] input={given!r} rule={rule} expected={expected!r} actual={actual!r}")


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("get", "GET"),
        ("POST", "POST"),
        ("", "UNKNOWN"),
        (None, "UNKNOWN"),
        # non-standard verbs a scanner might send collapse to a single value
        ("PROPFIND", METHOD_OTHER),
        ("BREW", METHOD_OTHER),
        ("totally-made-up", METHOD_OTHER),
        ("123", METHOD_OTHER),
        # the nine standard verbs are always kept
        ("head", "HEAD"),
        ("patch", "PATCH"),
        ("options", "OPTIONS"),
    ],
)
def test_normalize_method(given: str | None, expected: str) -> None:
    actual = normalize_method(given)
    _report("method", given, "standard verb upper-cased; empty -> UNKNOWN; else OTHER", expected, actual)
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
    [
        (200, "200"),
        (404, "404"),
        (503, "503"),
        (None, STATUS_NONE),
        # non-standard / out-of-range codes collapse instead of opening series
        (0, STATUS_OTHER),
        (99, STATUS_OTHER),
        (600, STATUS_OTHER),
        (612, STATUS_OTHER),
        (999, STATUS_OTHER),
    ],
)
def test_normalize_status(given: int | None, expected: str) -> None:
    actual = normalize_status(given)
    _report("status", given, "100-599 -> 3-digit string; None -> none; else other", expected, actual)
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


# --------------------------------------------------------------------------- #
# result classification (dashboard-ready six-class split)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("status", "raised", "timed_out", "disconnected", "expected"),
    [
        # complete responses split by status range
        (200, False, False, False, RESULT_SUCCESS),
        (204, False, False, False, RESULT_SUCCESS),
        (301, False, False, False, RESULT_SUCCESS),
        (100, False, False, False, RESULT_SUCCESS),
        (400, False, False, False, RESULT_CLIENT_ERROR),
        (404, False, False, False, RESULT_CLIENT_ERROR),
        (429, False, False, False, RESULT_CLIENT_ERROR),
        (500, False, False, False, RESULT_SERVER_ERROR),
        (502, False, False, False, RESULT_SERVER_ERROR),
        (503, False, False, False, RESULT_SERVER_ERROR),
        # non-standard completed codes are treated as server-side failures
        (612, False, False, False, RESULT_SERVER_ERROR),
        (999, False, False, False, RESULT_SERVER_ERROR),
        (0, False, False, False, RESULT_SERVER_ERROR),
        # failure modes
        (None, True, False, False, RESULT_EXCEPTION),
        (500, True, False, False, RESULT_EXCEPTION),
        (None, False, True, False, RESULT_TIMEOUT),
        (504, False, True, False, RESULT_TIMEOUT),
        (None, False, False, True, RESULT_CLIENT_DISCONNECT),
        (200, False, False, True, RESULT_CLIENT_DISCONNECT),
        (None, False, False, False, RESULT_EXCEPTION),
        # precedence: timeout > disconnect > exception > status classes
        (None, True, True, True, RESULT_TIMEOUT),
        (None, True, False, True, RESULT_CLIENT_DISCONNECT),
        (404, True, False, False, RESULT_EXCEPTION),
        (503, False, True, False, RESULT_TIMEOUT),
    ],
)
def test_classify_result(
    status: int | None, raised: bool, timed_out: bool, disconnected: bool, expected: str
) -> None:
    actual = classify_result(status, raised=raised, timed_out=timed_out, disconnected=disconnected)
    given = dict(status=status, raised=raised, timed_out=timed_out, disconnected=disconnected)
    _report(
        "result",
        given,
        "timeout>disconnect>exception>5xx/nonstandard=server_error>4xx=client_error>success",
        expected,
        actual,
    )
    assert actual == expected
    assert actual in ALL_RESULTS


def test_overflow_sentinel_is_stable() -> None:
    _report("overflow", LABEL_OVERFLOW, "fixed reserved fallback label value", "__overflow__", LABEL_OVERFLOW)
    assert LABEL_OVERFLOW == "__overflow__"
