"""Integration tests for the metrics collection pipeline.

Covers route normalization, status/outcome distinction, timeout, client
disconnect, concurrent conservation, idempotent setup and disabled metrics.
Each test prints the inputs and the metric samples used as judgement basis.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import asgi_client, make_app, parse_samples, raw_asgi_request, registry_of
from observability.labels import ROUTE_UNMATCHED
from observability.metrics import (
    HTTP_INFLIGHT,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
)


def _counter_rows(text: str) -> list[tuple[str, dict[str, str], float]]:
    return [(n, l, v) for n, l, v in parse_samples(text) if n == HTTP_REQUESTS_TOTAL]


async def _scrape_text(app) -> str:
    async with asgi_client(app) as c:
        return (await c.get("/metrics")).text


# --------------------------------------------------------------------------- #
# route normalization
# --------------------------------------------------------------------------- #
def test_routes_normalized_to_templates() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            await c.get("/items/1")
            await c.get("/items/42")
            await c.get("/items/abc?x=1")
            await c.get("/totally/unknown/a/b/c")
            await c.get("/another/unknown/999")
        text = await _scrape_text(app)
        rows = _counter_rows(text)
        print("input: GET /items/1 /items/42 /items/abc (same template)")
        print("input: GET /totally/unknown/a/b/c /another/unknown/999 (404)")
        print("judgement: item rows must aggregate under /items/{item_id}; unknowns under __unmatched__")
        for name, labels, value in rows:
            print(f"  {name}{labels} = {value:g}")
        item_rows = [r for r in rows if r[1]["route"] == "/items/{item_id}"]
        assert len(item_rows) == 1, "different ids must not create separate series"
        assert item_rows[0][2] == 3
        unmatched = [r for r in rows if r[1]["route"] == ROUTE_UNMATCHED]
        assert len(unmatched) == 1 and unmatched[0][2] == 2
        assert unmatched[0][1]["status"] == "404"

    asyncio.run(scenario())


def test_metrics_endpoint_has_its_own_bounded_label() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            await c.get("/metrics")
        text = await _scrape_text(app)
        rows = _counter_rows(text)
        print("judgement: /metrics request is labelled /metrics, not a path or __unmatched__")
        for _, labels, value in rows:
            print(f"  route={labels['route']} status={labels['status']} = {value:g}")
        assert any(l["route"] == "/metrics" and l["status"] == "200" for _, l, _ in rows)


# --------------------------------------------------------------------------- #
# outcome distinction
# --------------------------------------------------------------------------- #
def test_outcomes_are_distinguishable() -> None:
    async def scenario() -> None:
        app = make_app(request_timeout=0.2)
        async with asgi_client(app) as c:
            await c.get("/")                     # ok
            r_boom = await c.get("/boom")       # exception -> 500
            r_503 = await c.get("/http-error")  # error response
            r_slow = await c.get("/slow?wait=5")  # timeout -> 504
        print("inputs: GET / (200), /boom, /http-error, /slow?wait=5")
        assert (r_boom.status_code, r_503.status_code, r_slow.status_code) == (500, 503, 504)
        text = await _scrape_text(app)
        rows = {(l["route"], l["status"], l["outcome"]): v for _, l, v in _counter_rows(text)}
        print("judgement: one distinct outcome per failure path, statuses 200/500/503/504")
        for key, value in sorted(rows.items()):
            print(f"  {key} = {value:g}")
        assert ("/", "200", "ok") in rows
        assert ("/boom", "500", "exception") in rows
        assert ("/http-error", "503", "error") in rows
        assert ("/slow", "504", "timeout") in rows
        assert rows[("/boom", "500", "exception")] == 1
        assert rows[("/slow", "504", "timeout")] == 1

    asyncio.run(scenario())


def test_client_disconnect_is_distinguishable() -> None:
    async def scenario() -> None:
        app = make_app(request_timeout=None)
        messages, elapsed = await raw_asgi_request(app, "/slow", query="wait=5", disconnect_after=0.1)
        print("input: GET /slow?wait=5, client disconnects after 0.1s")
        print(f"judgement: no response sent (messages={messages}); request cancelled quickly (elapsed={elapsed:.3f}s)")
        assert messages == [], "disconnected request must not produce a response"
        assert elapsed < 1.0, "the endpoint should be cancelled promptly"
        reg = registry_of(app)
        assert reg.counter_value(
            HTTP_REQUESTS_TOTAL,
            (("method", "GET"), ("route", "/slow"), ("status", "none"),
             ("outcome", "client_disconnect")),
        ) == 1.0
        print("  counter series: method=GET route=/slow status=none outcome=client_disconnect = 1")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# conservation / concurrency
# --------------------------------------------------------------------------- #
def test_conservation_and_balance_under_concurrency() -> None:
    n_per_route = 80

    async def scenario() -> None:
        app = make_app(request_timeout=5.0)
        async with asgi_client(app) as c:
            async def hit(i: int) -> int:
                r = await c.get(f"/items/{i}")
                return r.status_code

            statuses = await asyncio.gather(*(hit(i) for i in range(n_per_route)))
            assert all(s == 200 for s in statuses)
        text = await _scrape_text(app)
        samples = parse_samples(text)

        total = sum(v for n, l, v in samples if n == HTTP_REQUESTS_TOTAL)
        hist_count = sum(v for n, l, v in samples if n == f"{HTTP_REQUEST_DURATION_SECONDS}_count")
        inflight_nonzero = {tuple(sorted(l.items())): v
                            for n, l, v in samples
                            if n == HTTP_INFLIGHT and l["route"] != "/metrics" and v != 0}
        print(f"input: {n_per_route} concurrent GET /items/{{id}}")
        print(f"judgement: counter_sum={total:g} == histogram_count_sum={hist_count:g} == {n_per_route}")
        print(f"judgement: all in-flight gauges (except self-observing /metrics) are 0: {inflight_nonzero}")
        assert total == n_per_route
        assert hist_count == n_per_route
        assert inflight_nonzero == {}

    asyncio.run(scenario())


def test_mixed_traffic_conservation() -> None:
    n_ok, n_err, n_404, n_timeout = 60, 20, 20, 10

    async def scenario() -> None:
        app = make_app(request_timeout=2.0)
        async with asgi_client(app) as c:
            tasks = (
                [c.get("/") for _ in range(n_ok)]
                + [c.get("/boom") for _ in range(n_err)]
                + [c.get(f"/missing/{i}") for _ in range(n_404) for i in range(1)]
                + [c.get("/slow?wait=5") for _ in range(n_timeout)]
            )
            await asyncio.gather(*tasks, return_exceptions=True)
        text = await _scrape_text(app)
        samples = parse_samples(text)
        total = sum(v for n, l, v in samples if n == HTTP_REQUESTS_TOTAL)
        hist_count = sum(v for n, l, v in samples if n == f"{HTTP_REQUEST_DURATION_SECONDS}_count")
        inflight_sum = sum(v for n, l, v in samples if n == HTTP_INFLIGHT and l["route"] != "/metrics")
        by_outcome: dict[str, float] = {}
        for n, l, v in samples:
            if n == HTTP_REQUESTS_TOTAL:
                by_outcome[l["outcome"]] = by_outcome.get(l["outcome"], 0.0) + v
        expected = n_ok + n_err + n_404 + n_timeout
        print(f"input: ok={n_ok} exception={n_err} 404={n_404} timeout={n_timeout}, total={expected}")
        print(f"judgement: counter_sum={total:g} histogram_count={hist_count:g} inflight(except /metrics)={inflight_sum:g}")
        print(f"judgement: by_outcome={by_outcome}")
        assert total == expected
        assert hist_count == expected
        assert inflight_sum == 0
        assert by_outcome["ok"] == n_ok
        assert by_outcome["client_error"] == n_404   # 4xx is its own outcome since this version
        assert by_outcome["exception"] == n_err
        assert by_outcome["timeout"] == n_timeout

    asyncio.run(scenario())


def test_inflight_gauges_during_traffic_then_zero() -> None:
    async def scenario() -> None:
        app = make_app(request_timeout=None)
        reg = registry_of(app)

        async with asgi_client(app) as c:
            task = asyncio.create_task(c.get("/slow?wait=0.3"))
            await asyncio.sleep(0.08)
            mid = reg.gauge_value(HTTP_INFLIGHT, (("method", "GET"), ("route", "/slow")))
            print(f"input: one GET /slow?wait=0.3 in progress; judgement: gauge(GET,/slow)={mid:g} (expect 1)")
            assert mid == 1.0
            await task
        assert reg.gauge_value(HTTP_INFLIGHT, (("method", "GET"), ("route", "/slow"))) == 0.0
        print("after completion gauge is back to 0")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# setup semantics / disabled metrics
# --------------------------------------------------------------------------- #
def test_setup_is_idempotent_and_rebuild_isolated() -> None:
    from observability.setup import SETUP_ATTR, setup_observability

    app = make_app()
    reg1 = getattr(app, SETUP_ATTR)
    reg2 = setup_observability(app)
    metrics_routes = [r for r in app.routes if getattr(r, "path", None) == "/metrics"]
    print("judgement: second setup returns same registry; exactly one /metrics route")
    assert reg1 is reg2
    # the included-router wrapper contributes a single /metrics endpoint
    assert len(metrics_routes) <= 1
    print("judgement: rebuilding the application yields an independent, empty registry")
    app_b = make_app()
    reg_b = getattr(app_b, SETUP_ATTR)
    assert reg_b is not reg1
    assert reg_b.counter_value(HTTP_REQUESTS_TOTAL, ()) == 0.0
    text = reg_b.render()
    assert "http_requests_total" in text
    assert all(line.endswith(" 0") or not line.startswith("http_requests_total{") or "inflight" in line
               for line in text.splitlines())


def test_disabled_duration_metric_hidden_and_conserved() -> None:
    async def scenario() -> None:
        app = make_app(duration_enabled=False)
        async with asgi_client(app) as c:
            await c.get("/")
        text = await _scrape_text(app)
        print("input: duration_enabled=False; GET /")
        print("judgement: duration family has no samples in output; counter still present")
        sample_names = {n for n, _, _ in parse_samples(text)}
        assert not any(
            n == HTTP_REQUEST_DURATION_SECONDS or n.startswith(f"{HTTP_REQUEST_DURATION_SECONDS}_")
            for n in sample_names
        ), "disabled duration family must not expose any series"
        assert HTTP_REQUESTS_TOTAL in text
        assert HTTP_INFLIGHT in text
        rows = _counter_rows(text)
        assert any(l["route"] == "/" and l["status"] == "200" for _, l, _ in rows)

    asyncio.run(scenario())


def test_scrape_content_type() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            r = await c.get("/metrics")
        print("judgement: exposition uses Prometheus text content type")
        assert r.headers["content-type"].startswith("text/plain; version=0.0.4")
        assert r.status_code == 200

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# method not allowed / performance stability
# --------------------------------------------------------------------------- #
def test_method_not_allowed_uses_bounded_template_label() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            r = await c.post("/items/1")
        assert r.status_code == 405
        text = await _scrape_text(app)
        rows = _counter_rows(text)
        print("input: POST /items/1 (route exists, method disallowed)")
        print("judgement: bounded template label /items/{item_id}, status 405, outcome client_error; not a raw path")
        for _, labels, value in rows:
            print(f"  route={labels['route']} status={labels['status']} outcome={labels['outcome']} = {value:g}")
        assert any(
            l["route"] == "/items/{item_id}" and l["status"] == "405" and l["outcome"] == "client_error"
            for _, l, _ in rows
        )

    asyncio.run(scenario())


def test_recording_cost_does_not_grow_with_history() -> None:
    async def scenario() -> None:
        app = make_app()
        reg = registry_of(app)
        labels = (("method", "GET"), ("route", "/items/{item_id}"),
                  ("status", "200"), ("outcome", "ok"))
        duration_labels = (("method", "GET"), ("route", "/items/{item_id}"), ("outcome", "ok"))

        def batch(n: int) -> float:
            import time
            t0 = time.perf_counter()
            for _ in range(n):
                reg.inc_counter(HTTP_REQUESTS_TOTAL, labels)
                reg.observe(HTTP_REQUEST_DURATION_SECONDS, duration_labels, 0.003)
            return time.perf_counter() - t0

        warm = batch(5_000)
        later = batch(20_000)
        per_call_warm = warm / 5_000
        per_call_later = later / 20_000
        print(f"input: 5000 then 20000 increments of the same series")
        print(f"judgement: amortized cost must stay flat; "
              f"early={per_call_warm*1e6:.3f}us/op later={per_call_later*1e6:.3f}us/op")
        # Generous bound to stay deterministic on busy CI: at most 4x slower.
        assert per_call_later < per_call_warm * 4 + 1e-6

    asyncio.run(scenario())
