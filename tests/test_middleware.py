"""Tests for route normalization, classification and failure channels."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.observability.middleware import (
    METHOD_OTHER,
    OUTCOME_CLIENT_DISCONNECT,
    OUTCOME_CLIENT_ERROR,
    OUTCOME_EXCEPTION,
    OUTCOME_SERVER_ERROR,
    OUTCOME_SUCCESS,
    OUTCOME_TIMEOUT,
    ROUTE_UNMATCHED,
    canonical_method,
    canonical_status,
    outcome_for_status,
    request_label_pairs,
    setup_observability,
)
from conftest import label_dict, logger


def test_method_normalization_is_closed() -> None:
    """Inputs: standard methods, lowercase, unknown verb, missing.

    Verdict basis: known verbs are upper-cased verbatim; everything else
    collapses to the single ``OTHER`` sentinel, never expanding labels.
    """
    cases = {
        "get": "GET", "POST": "POST", "pAtCh": "PATCH",
        "PROPFIND": METHOD_OTHER, "": METHOD_OTHER, None: METHOD_OTHER,
    }
    for raw, expected in cases.items():
        logger.info("INPUT method=%r -> expecting %r", raw, expected)
        assert canonical_method(raw) == expected
    logger.info("VERDICT all %d method inputs mapped into the closed set",
                len(cases))


def test_status_normalization_is_bounded_but_preserves_information() -> None:
    """Inputs: literal documented codes, exotic code, non-numeric outcome.

    Verdict basis: documented codes (499/504 included) are emitted as-is;
    any other code collapses to its class (e.g. 599 -> 5xx), so the value
    space is finite while the failure class stays visible.
    """
    cases = {200: "200", 404: "404", 405: "405", 499: "499", 504: "504",
             599: "5xx", 418: "418", 299: "2xx", 99: "0xx", None: "0xx"}
    for raw, expected in cases.items():
        logger.info("INPUT status=%r -> expecting %r", raw, expected)
        assert canonical_status(raw) == expected
    logger.info("VERDICT status vocabulary bounded: literals + class buckets")


def test_outcome_mapping_distinguishes_error_classes() -> None:
    """Inputs: status labels across all classes.

    Verdict basis: 1xx-3xx success, 4xx client_error, 5xx server_error;
    exception/disconnect/timeout are *separate* outcome values.
    """
    assert outcome_for_status("200") == OUTCOME_SUCCESS
    assert outcome_for_status("301") == OUTCOME_SUCCESS
    assert outcome_for_status("404") == OUTCOME_CLIENT_ERROR
    assert outcome_for_status("503") == OUTCOME_SERVER_ERROR
    distinct = {
        OUTCOME_SUCCESS, OUTCOME_CLIENT_ERROR, OUTCOME_SERVER_ERROR,
        OUTCOME_EXCEPTION, OUTCOME_CLIENT_DISCONNECT, OUTCOME_TIMEOUT,
    }
    logger.info("VERDICT distinct outcome values=%s", sorted(distinct))
    assert len(distinct) == 6


def test_label_pairs_cover_every_failure_channel() -> None:
    """Input: the pre-registered (status, outcome) combinations.

    Verdict basis: 499/disconnect, 504/timeout and 500/exception must all
    be present before any traffic exists, otherwise the first such event
    would hit the bounded-label guard instead of being recorded.
    """
    pairs = set(request_label_pairs())
    required = {
        ("499", OUTCOME_CLIENT_DISCONNECT),
        ("504", OUTCOME_TIMEOUT),
        ("500", OUTCOME_EXCEPTION),
        ("200", OUTCOME_SUCCESS),
        ("404", OUTCOME_CLIENT_ERROR),
        ("503", OUTCOME_SERVER_ERROR),
    }
    missing = required - pairs
    logger.info("INPUT required pairs=%s", sorted(required))
    logger.info("VERDICT missing pairs=%s of %s registered", missing, len(pairs))
    assert not missing


@pytest.fixture
def instrumented_client():
    app = FastAPI()

    @app.get("/items/{item_id}")
    async def read_item(item_id: int) -> dict[str, int]:
        return {"item_id": item_id}

    @app.get("/error")
    async def raise_error() -> None:
        raise RuntimeError("boom")

    @app.get("/http-error/{code}")
    async def raise_http_error(code: int) -> None:
        from fastapi import HTTPException
        raise HTTPException(status_code=code, detail="x")

    setup_observability(app)
    return TestClient(app, raise_server_exceptions=False), app


def _nonzero_counters(app: FastAPI) -> dict[tuple[str, ...], float]:
    text = app.state.observability_settings.registry.render()
    out: dict[tuple[str, ...], float] = {}
    for line in text.splitlines():
        if not line.startswith("http_requests_total{"):
            continue
        labels_part = line[line.index("{") + 1:line.rindex("}")]
        labels = label_dict(labels_part)
        value = float(line.rsplit(" ", 1)[1])
        if value:
            out[tuple(labels[k] for k in
                      ("method", "route", "status", "outcome"))] = value
    return out


def test_route_template_normalization(instrumented_client) -> None:
    """Inputs: /items/1, /items/2 (distinct concrete paths) and junk paths.

    Verdict basis: both item requests aggregate under the single template
    ``/items/{item_id}`` (count 2); unknown paths aggregate under
    ``unmatched`` and never produce per-path labels.
    """
    client, app = instrumented_client
    logger.info("INPUT requests: GET /items/1, GET /items/2, "
                "GET /junk/a, GET /junk/b")
    client.get("/items/1")
    client.get("/items/2")
    client.get("/junk/a")
    client.get("/junk/b")

    counters = _nonzero_counters(app)
    item_key = ("GET", "/items/{item_id}", "200", OUTCOME_SUCCESS)
    unmatched = {k: v for k, v in counters.items()
                 if k[1] == ROUTE_UNMATCHED}
    logger.info("VERDICT item series=%s value=%s", item_key,
                counters.get(item_key))
    logger.info("VERDICT unmatched series=%s", unmatched)
    assert counters.get(item_key) == 2.0
    assert all(k[2] == "404" and k[3] == OUTCOME_CLIENT_ERROR
               for k in unmatched)
    leaked = [k for k in counters if "/items/1" in k[1] or "/items/2" in k[1]]
    assert leaked == []


def test_method_not_allowed_keeps_path_template(instrumented_client) -> None:
    """Input: POST /items/3 while only GET is registered.

    Verdict basis: the metric keeps the template ``/items/{item_id}`` with
    status 405 (matched path, rejected method), rather than ``unmatched``.
    """
    client, app = instrumented_client
    logger.info("INPUT POST /items/3 against a GET-only route")
    response = client.post("/items/3")
    logger.info("VERDICT http status=%s", response.status_code)
    counters = _nonzero_counters(app)
    key = ("POST", "/items/{item_id}", "405", OUTCOME_CLIENT_ERROR)
    assert response.status_code == 405
    assert counters.get(key) == 1.0
    logger.info("VERDICT recorded %s=%s", key, counters[key])


def test_exception_distinguishable_from_http_500(instrumented_client) -> None:
    """Inputs: GET /error (uncaught RuntimeError) and GET /http-error/500
    (handled HTTPException producing a real 500).

    Verdict basis: both are status 500-class on their own routes, but the
    outcomes differ: ``exception`` vs ``server_error``.
    """
    client, app = instrumented_client
    logger.info("INPUT GET /error (RuntimeError) and GET /http-error/500")
    r1 = client.get("/error")
    r2 = client.get("/http-error/500")
    logger.info("VERDICT http statuses: %s, %s", r1.status_code, r2.status_code)
    counters = _nonzero_counters(app)
    exc_key = ("GET", "/error", "500", OUTCOME_EXCEPTION)
    http_key = ("GET", "/http-error/{code}", "500", OUTCOME_SERVER_ERROR)
    assert counters.get(exc_key) == 1.0
    assert counters.get(http_key) == 1.0
    logger.info("VERDICT %s=%s ; %s=%s",
                exc_key, counters.get(exc_key), http_key, counters.get(http_key))

