"""End-to-end tests for cardinality protection, result classes and the switch.

Exercises the full ASGI middleware + registry stack:

* the per-family cap reached through real HTTP traffic (series bounded,
  totals still conserved, fallback and self-monitoring counters populated);
* non-standard status codes collapsing to ``status="other"``;
* the six ``result`` classes being distinguishable and summing to the total;
* scanner-style junk methods not opening new method series;
* the global switch hiding everything (self-monitoring included).
"""
from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.responses import Response

from conftest import asgi_client, make_app, parse_samples, raw_asgi_request, registry_of
from observability.metrics import (
    HTTP_INFLIGHT,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    OBS_OVERFLOW_ACTIVE,
    OBS_OVERFLOW_TOTAL,
    OBS_SERIES,
    OBS_SERIES_CAP,
)
from observability.setup import setup_observability


def _rows(text: str, name: str = HTTP_REQUESTS_TOTAL):
    return [(l, v) for n, l, v in parse_samples(text) if n == name]


async def _scrape(app) -> str:
    async with asgi_client(app) as c:
        return (await c.get("/metrics")).text


def _weird_status_app(max_series: int | None = 500) -> FastAPI:
    app = FastAPI()
    setup_observability(app, max_series=max_series, request_timeout=1.0)

    @app.get("/weird")
    def weird() -> Response:
        # an upstream/proxy-style non-standard code
        return Response(status_code=612)

    return app


# --------------------------------------------------------------------------- #
# cap reached through real traffic
# --------------------------------------------------------------------------- #
def test_cap_converges_real_traffic_and_conserves() -> None:
    async def scenario() -> None:
        # cap=2 -> at most one natural counter series plus the fallback
        app = make_app(max_series=2, request_timeout=1.0)
        async with asgi_client(app) as c:
            await c.get("/")                 # 200 success      (natural)
            await c.get("/items/1")         # 200 success
            await c.get("/boom")            # 500 exception
            await c.get("/http-error")      # 503 server_error
            await c.get("/nope/1")          # 404 client_error
            await c.get("/nope/2")          # 404 client_error
            await c.get("/slow?wait=5")     # 504 timeout
        n_requests = 7
        reg = registry_of(app)
        text = await _scrape(app)
        rows = _rows(text)
        series_count = reg.family_series_count(HTTP_REQUESTS_TOTAL)
        total = sum(v for _, v in rows)
        fallback = sum(v for l, v in rows if l["route"] == "__overflow__")
        hist_count = sum(v for n, l, v in parse_samples(text)
                         if n == f"{HTTP_REQUEST_DURATION_SECONDS}_count")
        overflow_total = sum(v for n, l, v in parse_samples(text)
                             if n == OBS_OVERFLOW_TOTAL and l["family"] == HTTP_REQUESTS_TOTAL)
        print(f"\n[cap-e2e] cap=2, {n_requests} mixed requests")
        print(f"  rule: series_count<=2 (actual {series_count}); counter_sum={total:g} "
              f"hist_count={hist_count:g}; fallback={fallback:g}; obs_overflow_total={overflow_total:g}")
        for l, v in rows:
            print(f"  {l} = {v:g}")
        assert series_count <= 2
        assert total == n_requests
        assert hist_count == n_requests  # histogram conserves even when it also overflows
        assert fallback >= 1
        assert overflow_total >= 1

    asyncio.run(scenario())


def test_inflight_returns_to_zero_when_counter_overflows() -> None:
    async def scenario() -> None:
        # tiny counter/histogram cap, lots of 404s (whose up-front probe and
        # post-hoc route labels differ): the uncapped in-flight gauge must
        # still return to zero for every pair
        app = make_app(max_series=2, request_timeout=1.0)
        async with asgi_client(app) as c:
            await c.get("/")
            for i in range(30):
                await c.get(f"/missing/{i}")
            await c.get("/boom")
        text = await _scrape(app)
        # exclude the self-observed /metrics scrape which is in-flight at
        # the instant it renders itself
        lingering = [
            (n, l, v)
            for n, l, v in parse_samples(text)
            if n == HTTP_INFLIGHT and l["route"] != "/metrics" and v != 0
        ]
        print(f"\n[inflight-zero] 32 requests, counter cap=2; "
              f"rule=all non-/metrics inflight gauges are zero after completion")
        for n, l, v in lingering:
            print(f"  LEAK: {n}{l} = {v}")
        assert lingering == []

    asyncio.run(scenario())


def test_self_monitoring_visible_alongside_traffic() -> None:
    async def scenario() -> None:
        app = make_app(max_series=3)
        async with asgi_client(app) as c:
            await c.get("/")
            await c.get("/boom")
            await c.get("/missing/a")
            await c.get("/missing/b")
            await c.get("/missing/c")
        text = await _scrape(app)
        samples = parse_samples(text)
        gauges = {
            (n, l.get("family")): v
            for n, l, v in samples
            if n in (OBS_SERIES, OBS_SERIES_CAP, OBS_OVERFLOW_ACTIVE, OBS_OVERFLOW_TOTAL)
        }
        print("\n[self-e2e] self-monitoring rows in one scrape:")
        for key, value in sorted(gauges.items()):
            print(f"  {key} = {value:g}")
        # the request counter must show cap/series/overflow state for the family
        assert gauges[(OBS_SERIES_CAP, HTTP_REQUESTS_TOTAL)] == 3
        assert gauges[(OBS_SERIES, HTTP_REQUESTS_TOTAL)] == 3
        assert gauges[(OBS_OVERFLOW_ACTIVE, HTTP_REQUESTS_TOTAL)] == 1
        assert gauges[(OBS_OVERFLOW_TOTAL, HTTP_REQUESTS_TOTAL)] >= 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# non-standard status codes
# --------------------------------------------------------------------------- #
def test_nonstandard_status_collapses_to_other() -> None:
    async def scenario() -> None:
        app = _weird_status_app()
        async with asgi_client(app) as c:
            r = await c.get("/weird")
        print(f"\n[status-other] input: GET /weird -> upstream status {r.status_code}")
        print("  rule: status label 'other'; outcome error; result server_error; one series")
        assert r.status_code == 612
        reg = registry_of(app)
        value = reg.counter_value(
            HTTP_REQUESTS_TOTAL,
            (("method", "GET"), ("route", "/weird"), ("status", "other"),
             ("outcome", "error"), ("result", "server_error")),
        )
        assert value == 1
        text = await _scrape(app)
        rows = _rows(text)
        for l, v in rows:
            print(f"  {l} = {v:g}")
        assert any(l["status"] == "other" and l["result"] == "server_error" for l, _ in rows)
        assert not any(l["status"] == "612" for l, _ in rows)

    asyncio.run(scenario())


def test_many_nonstandard_codes_open_one_series() -> None:
    async def scenario() -> None:
        app = FastAPI()
        setup_observability(app, max_series=None)

        @app.get("/code/{code}")
        def code(code: int) -> Response:
            return Response(status_code=code)

        async with asgi_client(app) as c:
            for s in (600, 612, 700, 777, 999, 99, 0):
                try:
                    await c.get(f"/code/{s}")
                except Exception:
                    pass  # transport may reject bizarre codes; middleware still records
        reg = registry_of(app)
        text = await _scrape(app)
        rows = _rows(text)
        other_rows = [r for r in rows if r[0]["status"] == "other"]
        print(f"\n[status-other-bounded] 7 wild codes; rule: single status='other' series")
        for l, v in other_rows:
            print(f"  {l} = {v:g}")
        # same route + same collapsed status + same outcome/result -> one row
        assert len({(l["route"], l["status"], l["outcome"], l["result"]) for l, _ in other_rows}) == 1

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# six result classes, conservation across the split
# --------------------------------------------------------------------------- #
def test_six_result_classes_distinguishable_and_conserved() -> None:
    async def scenario() -> None:
        app = make_app(request_timeout=0.3)
        async with asgi_client(app) as c:
            await c.get("/")                  # success
            await c.get("/items/9")          # success
            await c.get("/missing/x")        # client_error (404)
            await c.post("/items/1")         # client_error (405)
            await c.get("/http-error")       # server_error (503)
            await c.get("/boom")             # exception (500)
            await c.get("/slow?wait=5")      # timeout (504)
        await raw_asgi_request(app, "/slow", query="wait=5", disconnect_after=0.05)
        text = await _scrape(app)
        rows = _rows(text)
        by_result: dict[str, float] = {}
        by_outcome: dict[str, float] = {}
        for l, v in rows:
            by_result[l["result"]] = by_result.get(l["result"], 0.0) + v
            by_outcome[l["outcome"]] = by_outcome.get(l["outcome"], 0.0) + v
        total = sum(v for _, v in rows)
        print("\n[results] 8 requests covering all six result classes:")
        print(f"  by_result={by_result}")
        print(f"  by_outcome={by_outcome}")
        print(f"  rule: six classes present; sum(by_result)={sum(by_result.values()):g} == total={total:g}")
        assert by_result["success"] == 2
        assert by_result["client_error"] == 2
        assert by_result["server_error"] == 1
        assert by_result["exception"] == 1
        assert by_result["timeout"] == 1
        assert by_result["client_disconnect"] == 1
        assert sum(by_result.values()) == total == 8

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# junk methods / paths from scanners stay bounded
# --------------------------------------------------------------------------- #
def test_scanner_junk_does_not_open_series() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            for verb in ("PROPFIND", "BREW", "FOOBAR", "123", "q" * 20):
                req = c.build_request(verb, "/anything/goes/here")
                try:
                    await c.send(req)
                except Exception:
                    pass
            for path in ("/a", "/b", "/c/d/e", "/wp-admin", "/.env"):
                await c.get(path)
        text = await _scrape(app)
        rows = _rows(text)
        methods = {l["method"] for l, _ in rows}
        routes = {l["route"] for l, _ in rows}
        print(f"\n[scanner] junk verbs + random paths")
        print(f"  methods seen: {sorted(methods)}")
        print(f"  routes seen:  {sorted(routes)}")
        print("  rule: verbs collapse to OTHER; unknown paths collapse to __unmatched__")
        assert methods <= {"OTHER", "GET"}
        assert "/anything/goes/here" not in routes
        assert "__unmatched__" in routes
        # all junk verbs share the single OTHER series, not one per verb
        other = [l for l, _ in rows if l["method"] == "OTHER"]
        assert len(other) <= 1  # same route/status/outcome/result combination

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# global enable switch
# --------------------------------------------------------------------------- #
def test_disabled_pipeline_renders_nothing_even_self_monitoring() -> None:
    async def scenario() -> None:
        app = make_app(enabled=False)
        async with asgi_client(app) as c:
            await c.get("/")
            r = await c.get("/metrics")
        print(f"\n[switch-off-e2e] enabled=False; GET / then GET /metrics "
              f"-> status {r.status_code}, body length {len(r.text)}")
        print("  rule: body empty; no http_* and no observability_* families")
        assert r.status_code == 200
        assert r.text == ""
        reg = registry_of(app)
        # traffic during the off window was not recorded
        assert reg.counter_value(
            HTTP_REQUESTS_TOTAL,
            (("method", "GET"), ("route", "/"), ("status", "200"),
             ("outcome", "ok"), ("result", "success")),
        ) == 0
        # back on: pipeline works again, self-monitoring included
        reg.set_pipeline_enabled(True)
        async with asgi_client(app) as c:
            await c.get("/")
            text = (await c.get("/metrics")).text
        assert "http_requests_total" in text
        assert "observability_metric_series" in text
        print("  re-enabled: http_* and observability_* families are back")

    asyncio.run(scenario())
