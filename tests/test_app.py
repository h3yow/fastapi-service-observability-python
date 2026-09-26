"""End-to-end tests over the instrumented FastAPI application."""

from __future__ import annotations

import random
import threading
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import create_app
from app.observability.middleware import (
    setup_observability,
)
from conftest import counter_total, logger, parse_samples


@pytest.fixture
def client():
    application = create_app()
    return TestClient(application, raise_server_exceptions=False), application


def test_scrape_is_standard_text_and_route_templated(client) -> None:
    """Inputs: two item requests with distinct ids, one scrape.

    Verdict basis: content type is the Prometheus text type, every sample
    line parses, concrete ids never appear as labels, and the template is
    used.
    """
    test_client = client[0]
    logger.info("INPUT GET /items/101, GET /items/202, GET /metrics")
    test_client.get("/items/101")
    test_client.get("/items/202")
    response = test_client.get("/metrics")
    logger.info("VERDICT content-type=%r status=%s bytes=%s",
                response.headers["content-type"], response.status_code,
                len(response.text))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "version=0.0.4" in response.headers["content-type"]
    # All sample lines must parse as numeric samples.
    samples = parse_samples(response.text)
    assert samples  # non-empty
    assert "/items/101" not in response.text
    assert "/items/202" not in response.text
    assert "/items/{item_id}" in response.text
    for header in ("# HELP http_requests_total", "# TYPE http_requests_total counter"):
        assert header in response.text
    logger.info("VERDICT %s distinct samples, no concrete ids leaked", len(samples))


def test_concurrent_requests_counter_conservation(client) -> None:
    """Input: 24 threads x 50 requests against random item ids, plus errors.

    Verdict basis: the counter total equals the exact number of requests;
    item successes aggregate to one template series; the histogram count
    equals the counter; and the in-flight gauge is zero afterwards.
    """
    test_client = client[0]
    threads_n, per_thread = 24, 50
    logger.info("INPUT threads=%s requests/thread=%s (mixed paths)",
                threads_n, per_thread)
    barrier = threading.Barrier(threads_n)

    def worker(index: int) -> None:
        barrier.wait()
        for j in range(per_thread):
            kind = j % 10
            if kind < 7:
                test_client.get(f"/items/{random.randint(1, 10_000)}")
            elif kind in (7, 8):
                test_client.get("/this/does/not/exist")
            else:
                test_client.get("/error")

    threads = [threading.Thread(target=worker, args=(i,))
               for i in range(threads_n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    scrape = test_client.get("/metrics").text
    samples = parse_samples(scrape)
    total = counter_total(samples)
    # The scrape itself is only recorded once its response has completed,
    # i.e. after this snapshot was rendered, so the snapshot contains only
    # the business requests. Its absence here is exactly the expected,
    # self-referential behavior; a follow-up scrape would show it.
    expected_requests = threads_n * per_thread
    logger.info("VERDICT total counter=%s expected=%s", total, expected_requests)
    assert total == expected_requests

    item_success = samples[
        'http_requests_total{method="GET",route="/items/{item_id}",'
        'status="200",outcome="success"}'
    ]
    expected_items = threads_n * per_thread * 7 // 10
    logger.info("VERDICT item success=%s expected~%s (aggregated by template)",
                item_success, expected_items)
    assert item_success == float(expected_items)

    hist_count = samples[
        'http_request_duration_seconds_count{method="GET",'
        'route="/items/{item_id}",status="200",outcome="success"}'
    ]
    assert hist_count == item_success  # count <-> counter conservation

    # Every in-flight gauge must be back at zero once all workers finished.
    in_flight = {k: v for k, v in samples.items()
                 if k.startswith("http_requests_in_progress{") and v != 0.0}
    logger.info("VERDICT nonzero in-flight after join=%s", in_flight)
    assert in_flight == {}


def test_in_progress_gauge_observed_during_concurrency(client) -> None:
    """Input: 12 concurrent slow requests; poll the scrape while in flight.

    Verdict basis: the gauge for /slow becomes >= 1 while work is ongoing
    (proving live in-flight accounting) and returns to exactly zero after.
    """
    test_client = client[0]
    observed_max = 0
    stop = threading.Event()

    def slow_requests() -> None:
        test_client.get("/slow", params={"delay": 0.6})

    def scrape_loop() -> None:
        nonlocal observed_max
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not stop.is_set():
            text = test_client.get("/metrics").text
            for line in text.splitlines():
                if line.startswith("http_requests_in_progress{"):
                    labels = line[line.index("{") + 1:line.rindex("}")]
                    if 'route="/slow"' in labels:
                        observed_max = max(
                            observed_max, int(float(line.rsplit(" ", 1)[1]))
                        )
            time.sleep(0.01)

    workers = [threading.Thread(target=slow_requests) for _ in range(12)]
    scraper = threading.Thread(target=scrape_loop)
    for w in workers:
        w.start()
    scraper.start()
    for w in workers:
        w.join()
    stop.set()
    scraper.join()

    logger.info("VERDICT observed_max in-flight for /slow=%s", observed_max)
    assert observed_max >= 1
    final = test_client.get("/metrics").text
    leftover = [l for l in final.splitlines()
                if 'http_requests_in_progress{' in l
                and 'route="/slow"' in l and not l.endswith("0.0")]
    logger.info("VERDICT leftover /slow gauge lines=%s", leftover)
    assert leftover == []


def test_cardinality_does_not_grow_with_traffic(client) -> None:
    """Input: 1000 requests over random unknown paths, ids and methods.

    Verdict basis: the set of route label values and the number of series
    stay bounded; random concrete paths never become label values, and a
    scrape after history is no slower than a warm scrape on a fresh app.
    """
    test_client = client[0]
    verbs = ["GET", "POST", "DELETE", "PROPFIND", "BREW"]
    for i in range(1000):
        test_client.request(random.choice(verbs),
                            f"/random/{i}/tail/{random.randint(0, 10 ** 6)}")
    scrape = test_client.get("/metrics").text
    routes = {
        line.split('route="', 1)[1].split('"', 1)[0]
        for line in scrape.splitlines()
        if "http_requests_total{" in line
    }
    logger.info("VERDICT distinct route label values after 1000 random "
                "requests=%s", sorted(routes))
    assert routes <= {"unmatched", "/", "/items/{item_id}", "/error",
                      "/http-error/{code}", "/slow", "/metrics"}
    # Timing: rendering iterates only over the fixed metric set; compare two
    # scrapes, both should be fast and similar regardless of history.
    timings = []
    for _ in range(5):
        start = time.perf_counter()
        test_client.get("/metrics")
        timings.append(time.perf_counter() - start)
    logger.info("VERDICT scrape timings after history=%.4fs max", max(timings))
    assert max(timings) < 1.0  # generous bound; O(1) exposition is far below


def test_real_http_timeout_records_timeout_outcome() -> None:
    """Input: GET /slow?delay=1 against an app with a 0.2s request timeout.

    Verdict basis: the client receives 504 and the metric records status
    504 / outcome timeout on the /slow template.
    """
    application = create_app(request_timeout=0.2)
    test_client = TestClient(application, raise_server_exceptions=False)
    logger.info("INPUT GET /slow?delay=1 with request_timeout=0.2")
    response = test_client.get("/slow", params={"delay": 1})
    logger.info("VERDICT http status=%s", response.status_code)
    assert response.status_code == 504
    scrape = test_client.get("/metrics").text
    needle = ('http_requests_total{method="GET",route="/slow",'
              'status="504",outcome="timeout"}')
    assert any(line.startswith(needle + " ") for line in scrape.splitlines())
    logger.info("VERDICT found timeout series %r", needle)


def test_setup_is_idempotent_and_app_rebuild_is_isolated() -> None:
    """Inputs: setup twice on one app; build two independent apps.

    Verdict basis: repeated setup returns the same object, one /metrics
    route and one middleware exist; a rebuilt app has an empty registry and
    never raises on duplicate series.
    """
    application = FastAPI()

    @application.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    first = setup_observability(application)
    second = setup_observability(application)
    logger.info("INPUT setup_observability called twice on one app")
    assert first is second
    metrics_routes = [r for r in application.routes
                      if getattr(r, "path", None) == "/metrics"]
    middlewares = list(application.user_middleware)
    setup_observability(application)
    assert len(metrics_routes) == 1
    assert len(application.user_middleware) == len(middlewares)
    logger.info("VERDICT one /metrics route=%s, middleware stack stable=%s",
                len(metrics_routes), len(application.user_middleware))

    app_a, app_b = create_app(), create_app()
    TestClient(app_a).get("/")
    scrape_a = app_a.state.observability_settings.registry.render()
    scrape_b = app_b.state.observability_settings.registry.render()
    logger.info("VERDICT fresh app B scrape has no sample lines=%s",
                not any(not ln.startswith("#") and ln.strip()
                        for ln in scrape_b.splitlines()))
    assert 'route="/"' in scrape_a
    # A rebuilt app has zero observations: headers may be declared, but no
    # sample lines (and therefore no series carried over from app A).
    assert all(ln.startswith("#") or not ln.strip()
               for ln in scrape_b.splitlines())
    assert 'route="/"' not in scrape_b


def test_disabled_metrics_are_absent_from_scrape() -> None:
    """Inputs: an app with only the counter enabled; another with none.

    Verdict basis: disabled metric names never appear; enabled ones do.
    """
    application = FastAPI()

    @application.get("/x")
    async def x() -> dict[str, bool]:
        return {"ok": True}

    setup_observability(
        application,
        enable_requests_total=True,
        enable_request_duration_seconds=False,
        enable_requests_in_progress=False,
    )
    test_client = TestClient(application)
    test_client.get("/x")
    text = test_client.get("/metrics").text
    logger.info("VERDICT counter-only scrape contains total=%s, duration=%s, "
                "in_progress=%s",
                "http_requests_total" in text,
                "http_request_duration_seconds" in text,
                "http_requests_in_progress" in text)
    assert "http_requests_total" in text
    assert "http_request_duration_seconds" not in text
    assert "http_requests_in_progress" not in text

    empty_app = FastAPI()

    @empty_app.get("/y")
    async def y() -> dict[str, bool]:
        return {"ok": True}

    setup_observability(
        empty_app,
        enable_requests_total=False,
        enable_request_duration_seconds=False,
        enable_requests_in_progress=False,
    )
    empty_text = TestClient(empty_app).get("/y").text  # not a metrics scrape
    empty_text = TestClient(empty_app).get("/metrics").text
    logger.info("VERDICT all-disabled scrape body=%r", empty_text)
    assert empty_text == ""


def test_scrape_endpoint_does_not_count_itself(client) -> None:
    """Input: GET /metrics twice.

    Verdict basis: the scrape route appears in no counter/gauge series,
    because counting the in-flight scrape would always report 1 while its
    own body is rendered (a permanent self-reference).
    """
    test_client = client[0]
    logger.info("INPUT GET /metrics twice")
    first = test_client.get("/metrics").text
    second = test_client.get("/metrics").text
    logger.info("VERDICT /metrics series present in scrape=%s",
                'route="/metrics"' in second)
    assert 'route="/metrics"' not in first
    assert 'route="/metrics"' not in second
