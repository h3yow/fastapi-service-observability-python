"""Unit tests for the concurrency-safe metrics registry.

Conservation property under concurrency: N threads each incrementing the same
counter M times must yield exactly N*M -- no lost or duplicated updates.
"""
from __future__ import annotations

import threading

from observability.registry import MetricsRegistry

N_THREADS = 16
N_INCREMENTS = 500


def _make_registry() -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.register_counter("reqs_total", "total requests", ("route", "status"))
    reg.register_gauge("inflight", "in-flight requests", ("route",))
    reg.register_histogram("latency_seconds", "latency", ("route",), (0.1, 0.5, 1.0))
    return reg


def test_counter_conservation_under_concurrency() -> None:
    reg = _make_registry()
    labels = (("route", "/items/{item_id}"), ("status", "200"))

    def worker() -> None:
        for _ in range(N_INCREMENTS):
            reg.inc_counter("reqs_total", labels)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = N_THREADS * N_INCREMENTS
    actual = reg.counter_value("reqs_total", labels)
    print(
        f"\n[conservation] threads={N_THREADS} increments/thread={N_INCREMENTS} "
        f"expected={expected} actual={actual} rule=no lost/duplicate updates"
    )
    assert actual == expected


def test_gauge_returns_to_zero_under_concurrency() -> None:
    reg = _make_registry()
    labels = (("route", "/"),)

    def worker() -> None:
        for _ in range(N_INCREMENTS):
            reg.inc_gauge("inflight", labels, 1.0)
            reg.inc_gauge("inflight", labels, -1.0)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    actual = reg.gauge_value("inflight", labels)
    print(f"\n[gauge-balance] expected=0 actual={actual} rule=inc/dec pairs must cancel")
    assert actual == 0.0


def test_histogram_counts_and_sum() -> None:
    reg = _make_registry()
    labels = (("route", "/"),)
    observations = [0.05, 0.2, 0.2, 0.7, 2.0]
    for v in observations:
        reg.observe("latency_seconds", labels, v)

    count, total = reg.histogram_stats("latency_seconds", labels)
    print(
        f"\n[histogram] observations={observations} expected_count={len(observations)} "
        f"expected_sum={sum(observations)} actual=({count}, {total})"
    )
    assert count == len(observations)
    assert abs(total - sum(observations)) < 1e-9

    text = reg.render()
    print(f"\n[histogram-render]\n{text}")
    # cumulative buckets: 0.05<=0.1; 0.05,0.2,0.2<=0.5; +0.7<=1.0; all<=+Inf
    assert 'latency_seconds_bucket{route="/",le="0.1"} 1' in text
    assert 'latency_seconds_bucket{route="/",le="0.5"} 3' in text
    assert 'latency_seconds_bucket{route="/",le="1"} 4' in text
    assert 'latency_seconds_bucket{route="/",le="+Inf"} 5' in text
    assert 'latency_seconds_count{route="/"} 5' in text


def test_disabled_metric_hidden_and_noop() -> None:
    reg = _make_registry()
    reg.inc_counter("reqs_total", (("route", "/"), ("status", "200")))
    reg.set_enabled("reqs_total", False)
    reg.inc_counter("reqs_total", (("route", "/"), ("status", "200")))  # must be a no-op

    text = reg.render()
    print(f"\n[disabled] render after disable:\n{text}")
    assert "reqs_total" not in text, "disabled metric must not appear in output"
    assert "inflight" in text and "latency_seconds" in text

    reg.set_enabled("reqs_total", True)
    value = reg.counter_value("reqs_total", (("route", "/"), ("status", "200")))
    print(f"[disabled] value after re-enable expected=1 actual={value} (increment while disabled dropped)")
    assert value == 1.0


def test_registration_is_idempotent() -> None:
    reg = _make_registry()
    # re-registering the identical spec must not fail or reset data
    reg.inc_counter("reqs_total", (("route", "/"), ("status", "200")))
    reg.register_counter("reqs_total", "total requests", ("route", "status"))
    reg.register_counter("reqs_total", "total requests", ("route", "status"))
    value = reg.counter_value("reqs_total", (("route", "/"), ("status", "200")))
    print(f"\n[idempotent] value after duplicate registration expected=1 actual={value}")
    assert value == 1.0


def test_render_format_and_escaping() -> None:
    reg = MetricsRegistry()
    reg.register_counter("weird_total", "help", ("route",))
    reg.inc_counter("weird_total", (("route", '/a"b\\c'),))
    text = reg.render()
    print(f"\n[format]\n{text}")
    assert text.startswith("# HELP weird_total help\n# TYPE weird_total counter\n")
    assert 'weird_total{route="/a\\"b\\\\c"} 1' in text
    assert text.endswith("\n")
