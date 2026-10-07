"""Tests for the series-cardinality cap, fallback series and self-monitoring.

Covers: the cap is never exceeded (including under thread-level concurrency),
folded recordings land in the fixed ``__overflow__`` fallback series with
correct accounting, conservation (no lost/duplicated counts) still holds,
the cap can be switched off, and the ``observability_*`` self-monitoring
families expose the protection layer's state while following the master
enable switch.  Each test prints its inputs and judgement basis.
"""
from __future__ import annotations

import asyncio
import threading

from conftest import asgi_client, make_app, parse_samples, raw_asgi_request, registry_of
from observability.labels import ROUTE_UNMATCHED
from observability.metrics import (
    DEFAULT_MAX_SERIES,
    HTTP_INFLIGHT,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    create_http_registry,
)
from observability.middleware import MetricsASGIMiddleware
from observability.registry import (
    OVERFLOW_LABEL_VALUE,
    SELF_OVERFLOW_TOTAL,
    SELF_RENDER_DURATION_SECONDS,
    SELF_SERIES_CURRENT,
    SELF_SERIES_LIMIT,
    MetricsRegistry,
)

# --------------------------------------------------------------------------- #
# registry-level cap behaviour
# --------------------------------------------------------------------------- #
def test_cap_never_exceeded_and_conserved_under_concurrency() -> None:
    n_threads, n_per_thread, max_series = 16, 200, 50
    reg = MetricsRegistry(max_series=max_series, self_monitoring=True)
    reg.register_counter("reqs_total", "total requests", ("route",))

    def worker(tid: int) -> None:
        for i in range(n_per_thread):
            reg.inc_counter("reqs_total", (("route", f"/t{tid}/r{i}"),))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total_writes = n_threads * n_per_thread
    series = reg.series_count()
    overflow = reg.overflow_count("reqs_total")
    fallback = reg.counter_value("reqs_total", (("route", OVERFLOW_LABEL_VALUE),))
    counter_sum = sum(reg._counters["reqs_total"].values())
    # one family -> one reserved fallback slot -> max_series-1 regular series
    regular = max_series - 1
    print(f"input: {n_threads} threads x {n_per_thread} distinct routes, max_series={max_series}")
    print(f"judgement: series={series:g} <= {max_series}; "
          f"regular={regular} fallback={fallback:g} == overflow={overflow:g} == {total_writes - regular}")
    print(f"judgement: counter_sum={counter_sum:g} == total_writes={total_writes} (conservation)")
    assert series == max_series
    assert series <= max_series
    assert fallback == total_writes - regular
    assert overflow == total_writes - regular
    assert counter_sum == total_writes


def test_fallback_series_uses_fixed_overflow_labels() -> None:
    reg = MetricsRegistry(max_series=3)
    reg.register_counter("reqs_total", "t", ("method", "route"))
    for i in range(6):
        reg.inc_counter("reqs_total", (("method", "GET"), ("route", f"/r{i}")))
    text = reg.render()
    print("input: 6 distinct routes, max_series=3 (1 counter family)")
    print(f"judgement: 2 regular series + the __overflow__ series fill the budget of 3:\n{text}")
    assert 'reqs_total{method="GET",route="/r0"} 1' in text
    assert 'reqs_total{method="GET",route="/r1"} 1' in text
    assert f'reqs_total{{method="{OVERFLOW_LABEL_VALUE}",route="{OVERFLOW_LABEL_VALUE}"}} 4' in text
    assert reg.series_count() == 3  # 2 regular + 1 fallback == budget, never more


def test_histogram_overflow_keeps_bucket_conservation() -> None:
    reg = MetricsRegistry(max_series=10)
    reg.register_histogram("latency_seconds", "lat", ("route",), (0.1, 1.0))
    n = 20
    for i in range(n):
        reg.observe("latency_seconds", (("route", f"/r{i}"),), 0.05)
    fb_count, fb_sum = reg.histogram_stats(
        "latency_seconds", (("route", OVERFLOW_LABEL_VALUE),))
    total_count = sum(data[-2] for data in reg._histograms["latency_seconds"]["series"].values())
    print(f"input: {n} distinct-route observations, max_series=10 (1 histogram family)")
    print(f"judgement: fallback_count={fb_count:g} == {n - 9}; "
          f"all_counts_sum={total_count:g} == {n} (bucket conservation)")
    assert reg.series_count() == 10
    assert fb_count == n - 9
    assert abs(fb_sum - (n - 9) * 0.05) < 1e-9
    assert total_count == n
    assert reg.overflow_count("latency_seconds") == n - 9


def test_gauge_fallback_stays_balanced() -> None:
    reg = MetricsRegistry(max_series=4)
    reg.register_gauge("inflight", "in-flight", ("route",))
    for i in range(10):
        labels = (("route", f"/r{i}"),)
        reg.inc_gauge("inflight", labels, 1.0)
        reg.inc_gauge("inflight", labels, -1.0)
    gauges = dict(reg._gauges["inflight"])
    print("input: 10 distinct routes inc+dec, max_series=4 (1 gauge family)")
    print(f"judgement: every gauge series (incl. fallback) is back to 0: {gauges}")
    assert reg.overflow_count("inflight") == 2 * (10 - 3)  # inc and dec both fold
    assert all(v == 0.0 for v in gauges.values())


def test_unlimited_when_cap_disabled() -> None:
    reg = MetricsRegistry(max_series=None, self_monitoring=True)
    reg.register_counter("reqs_total", "t", ("route",))
    for i in range(500):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"),))
    text = reg.render()
    print("input: 500 distinct routes, max_series=None (cap disabled)")
    print(f"judgement: series={reg.series_count():g} == 500; no __overflow__; limit=+Inf")
    assert reg.series_count() == 500
    assert reg.overflow_count("reqs_total") == 0.0
    assert OVERFLOW_LABEL_VALUE not in text
    assert f"{SELF_SERIES_LIMIT} +Inf" in text


# --------------------------------------------------------------------------- #
# self-monitoring families
# --------------------------------------------------------------------------- #
def test_self_monitoring_reports_cap_state() -> None:
    reg = MetricsRegistry(max_series=7, self_monitoring=True)
    reg.register_counter("reqs_total", "t", ("route",))
    reg.register_gauge("inflight", "g", ("route",))
    for i in range(20):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"),))
    samples = {(n, tuple(sorted(l.items()))): v for n, l, v in parse_samples(reg.render())}
    limit = samples[(SELF_SERIES_LIMIT, ())]
    current_total = sum(v for (n, _), v in samples.items() if n == SELF_SERIES_CURRENT)
    overflow = samples[(SELF_OVERFLOW_TOTAL, (("metric", "reqs_total"),))]
    render_seconds = samples[(SELF_RENDER_DURATION_SECONDS, ())]
    print("input: max_series=7, 2 families, 20 distinct counter routes")
    print(f"judgement: limit={limit:g} == 7; current_total={current_total:g} <= 7; "
          f"overflow={overflow:g} == {reg.overflow_count('reqs_total'):g}; "
          f"render_seconds={render_seconds} >= 0")
    assert limit == 7
    assert current_total <= 7
    assert current_total == reg.series_count()
    assert overflow == reg.overflow_count("reqs_total") > 0
    assert render_seconds >= 0.0


def test_self_monitoring_follows_master_switch() -> None:
    reg = create_http_registry(enabled=False)
    reg.inc_counter(HTTP_REQUESTS_TOTAL, (("method", "GET"),))
    text_off = reg.render()
    print("input: create_http_registry(enabled=False), one write, render")
    print(f"judgement: exposition is empty (no http_*, no observability_*): {text_off!r}")
    assert text_off == ""

    reg.set_all_enabled(True)
    text_on = reg.render()
    print("judgement: after set_all_enabled(True) both http_* and observability_* appear")
    assert HTTP_REQUESTS_TOTAL in text_on
    assert SELF_SERIES_LIMIT in text_on
    # writes done while disabled are not back-filled
    assert reg.counter_value(HTTP_REQUESTS_TOTAL, (("method", "GET"),)) == 0.0


# --------------------------------------------------------------------------- #
# middleware / application level
# --------------------------------------------------------------------------- #
def _add_routes(app, prefix: str, n: int) -> None:
    def handler() -> dict[str, bool]:
        return {"ok": True}

    for i in range(n):
        app.add_api_route(f"/{prefix}{i}", handler, methods=["GET"])


def test_middleware_overflow_conservation_and_bounded_exposition() -> None:
    n_routes, max_series = 30, 20

    async def scenario() -> None:
        app = make_app(max_series=max_series)
        _add_routes(app, "r", n_routes)
        async with asgi_client(app) as c:
            for i in range(n_routes):
                r = await c.get(f"/r{i}")
                assert r.status_code == 200
            text = (await c.get("/metrics")).text
        samples = parse_samples(text)
        reg = registry_of(app)

        total = sum(v for n, l, v in samples if n == HTTP_REQUESTS_TOTAL)
        hist_count = sum(v for n, l, v in samples if n == f"{HTTP_REQUEST_DURATION_SECONDS}_count")
        overflow_rows = [v for n, l, v in samples
                         if n == HTTP_REQUESTS_TOTAL and l["route"] == OVERFLOW_LABEL_VALUE]
        series_current = {l["metric"]: v for n, l, v in samples if n == SELF_SERIES_CURRENT}
        limit = [v for n, l, v in samples if n == SELF_SERIES_LIMIT][0]
        fallback_labels = tuple((k, OVERFLOW_LABEL_VALUE)
                                for k in ("method", "route", "status", "outcome"))
        fallback_value = reg.counter_value(HTTP_REQUESTS_TOTAL, fallback_labels)
        overflow = reg.overflow_count(HTTP_REQUESTS_TOTAL)
        inflight_left = {k: v for k, v in reg._gauges[HTTP_INFLIGHT].items() if v != 0}
        print(f"input: {n_routes} distinct routes, max_series={max_series}")
        print(f"judgement: exposed series={reg.series_count():g} <= {max_series}; "
              f"counter_sum={total:g} == histogram_count={hist_count:g} == {n_routes}")
        print(f"judgement: rendered fallback rows={overflow_rows}; "
              f"fallback_value={fallback_value:g} == overflow_count={overflow:g}; "
              f"series_current={series_current}; limit={limit:g}; inflight_left={inflight_left}")
        assert reg.series_count() <= max_series
        assert limit == max_series
        assert all(v <= max_series for v in series_current.values())
        assert total == n_routes            # folded requests are still counted
        assert hist_count == n_routes       # ... and still observed in the histogram
        assert len(overflow_rows) == 1 and overflow_rows[0] > 0
        # the scrape itself is recorded after rendering, so compare
        # registry-side values read after all requests settled
        assert fallback_value == overflow > 0
        assert inflight_left == {}          # fallback gauge also returns to 0

    asyncio.run(scenario())


def test_self_monitoring_visible_in_scrape_with_default_config() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            await c.get("/")
            text = (await c.get("/metrics")).text
        samples = parse_samples(text)
        names = {n for n, _, _ in samples}
        limit = [v for n, l, v in samples if n == SELF_SERIES_LIMIT][0]
        render_seconds = [v for n, l, v in samples if n == SELF_RENDER_DURATION_SECONDS][0]
        print("input: default app, GET / then GET /metrics")
        print(f"judgement: observability_* present; limit={limit:g} == default {DEFAULT_MAX_SERIES}; "
              f"render_seconds={render_seconds} >= 0")
        assert {SELF_SERIES_CURRENT, SELF_SERIES_LIMIT, SELF_OVERFLOW_TOTAL,
                SELF_RENDER_DURATION_SECONDS} <= names
        assert limit == DEFAULT_MAX_SERIES
        assert render_seconds >= 0.0

    asyncio.run(scenario())


def test_master_switch_off_hides_everything_in_scrape() -> None:
    async def scenario() -> None:
        app = make_app(enabled=False)
        async with asgi_client(app) as c:
            await c.get("/")
            r = await c.get("/metrics")
        samples = parse_samples(r.text)
        print("input: enabled=False; GET / then GET /metrics")
        print(f"judgement: scrape is 200 with zero samples (no http_*, no observability_*): "
              f"status={r.status_code} samples={len(samples)}")
        assert r.status_code == 200
        assert samples == []

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# non-standard status codes collapse to bounded labels
# --------------------------------------------------------------------------- #
def _status_app(status: int):
    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"x"})
    return inner


def test_non_standard_status_collapses_to_other() -> None:
    async def scenario() -> None:
        reg = create_http_registry()
        app_777 = MetricsASGIMiddleware(_status_app(777), reg, request_timeout=None)
        app_99 = MetricsASGIMiddleware(_status_app(99), reg, request_timeout=None)
        await raw_asgi_request(app_777, "/proxy/weird")
        await raw_asgi_request(app_99, "/proxy/weirder")
        rows = [
            (l, v) for n, l, v in parse_samples(reg.render())
            if n == HTTP_REQUESTS_TOTAL and l["route"] == ROUTE_UNMATCHED
        ]
        print("input: raw ASGI apps returning status 777 and 99 (non-standard)")
        print("judgement: both collapse to status=other; 777 -> error, 99 -> ok")
        for labels, value in rows:
            print(f"  status={labels['status']} outcome={labels['outcome']} = {value:g}")
        by_outcome = {l["outcome"]: v for l, v in rows}
        assert all(l["status"] == "other" for l, _ in rows)
        assert by_outcome == {"error": 1.0, "ok": 1.0}
        # and the two requests are still fully accounted for
        assert sum(by_outcome.values()) == 2.0

    asyncio.run(scenario())
