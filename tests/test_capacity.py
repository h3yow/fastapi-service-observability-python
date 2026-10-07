"""Unit tests for the cardinality cap, fallback convergence and self-monitoring.

Every test prints its input shape and the judgement basis (series counts,
fallback value, rendered self-monitoring samples) so the evidence is visible
in a ``pytest -s`` log.
"""
from __future__ import annotations

import threading

import pytest

from observability.labels import LABEL_OVERFLOW
from observability.metrics import (
    DEFAULT_MAX_SERIES,
    HTTP_INFLIGHT,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_TOTAL,
    OBS_OVERFLOW_ACTIVE,
    OBS_OVERFLOW_TOTAL,
    OBS_RENDER_SECONDS,
    OBS_SERIES,
    OBS_SERIES_CAP,
    create_http_registry,
)
from observability.registry import MetricsRegistry


def _make_capped_registry(max_series: int | None) -> MetricsRegistry:
    reg = MetricsRegistry()
    reg.register_counter("reqs_total", "total", ("route", "status"), max_series=max_series)
    return reg


# --------------------------------------------------------------------------- #
# cap basics
# --------------------------------------------------------------------------- #
def test_natural_series_fill_budget_without_overflow() -> None:
    cap = 3
    reg = _make_capped_registry(cap)
    for i in range(cap - 1):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"), ("status", "200")))
    count = reg.family_series_count("reqs_total")
    active = reg.family_is_overflowing("reqs_total")
    print(f"\n[cap-fill] cap={cap} distinct={cap - 1} rule=cap-1 natural keys before fallback")
    print(f"  series_count={count} overflow_active={active}")
    assert count == cap - 1
    assert active is False


def test_overflow_series_count_never_exceeds_cap() -> None:
    cap = 5
    reg = _make_capped_registry(cap)
    for i in range(1000):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"), ("status", "200")))
    count = reg.family_series_count("reqs_total")
    print(f"\n[cap-hard] cap={cap} input=1000 distinct routes "
          f"rule=exposed series must never exceed cap; series_count={count}")
    assert count == cap
    assert count <= cap


def test_overflow_conserves_total_and_counts_collapsed_writes() -> None:
    cap = 4
    reg = _make_capped_registry(cap)
    n = 500
    for i in range(n):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"), ("status", "200")))
    natural_total = sum(
        reg.counter_value("reqs_total", (("route", f"/r{i}"), ("status", "200")))
        for i in range(cap - 1)
    )
    fallback = reg.counter_value(
        "reqs_total", (("route", LABEL_OVERFLOW), ("status", LABEL_OVERFLOW))
    )
    print(f"\n[conserve] cap={cap} input={n} distinct natural_total={natural_total:g} "
          f"fallback={fallback:g} sum={natural_total + fallback:g}")
    print("  rule=natural + fallback == total requests; exactly cap-1 natural series")
    assert natural_total == cap - 1
    assert fallback == n - (cap - 1)
    assert natural_total + fallback == n


def test_repeated_overflow_keys_aggregate_into_one_fallback() -> None:
    cap = 2
    reg = _make_capped_registry(cap)
    # one admitted natural key, then many *different* overflowing keys
    reg.inc_counter("reqs_total", (("route", "/ok"), ("status", "200")))
    for i in range(50):
        reg.inc_counter("reqs_total", (("route", f"/x{i}"), ("status", "200")))
    for i in range(50):
        reg.inc_counter("reqs_total", (("route", f"/y{i}"), ("status", "418")))
    fallback = reg.counter_value(
        "reqs_total", (("route", LABEL_OVERFLOW), ("status", LABEL_OVERFLOW))
    )
    count = reg.family_series_count("reqs_total")
    print(f"\n[fallback-one] cap=2 input=1 natural + 100 distinct overflow combinations")
    print(f"  rule=single fallback series holds all 100; series_count={count} fallback={fallback:g}")
    assert count == 2
    assert fallback == 100


def test_cap_eq_one_leaves_only_the_fallback() -> None:
    reg = _make_capped_registry(1)
    for i in range(10):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"), ("status", "200")))
    count = reg.family_series_count("reqs_total")
    fallback = reg.counter_value(
        "reqs_total", (("route", LABEL_OVERFLOW), ("status", LABEL_OVERFLOW))
    )
    print(f"\n[cap-one] cap=1 input=10 distinct rule=only the fallback series exists")
    print(f"  series_count={count} fallback={fallback:g}")
    assert count == 1
    assert fallback == 10


def test_histogram_cap_conserves_count_and_buckets() -> None:
    reg = MetricsRegistry()
    reg.register_histogram("lat", "latency", ("route",), (0.1, 0.5, 1.0), max_series=3)
    n = 30
    for i in range(n):
        reg.observe("lat", (("route", f"/r{i}"),), 0.05 if i % 2 else 0.7)
    fallback_count, fallback_sum = reg.histogram_stats(
        "lat", (("route", LABEL_OVERFLOW),)
    )
    natural_counts = [
        reg.histogram_stats("lat", (("route", f"/r{i}"),))[0] for i in range(2)
    ]
    total_count = fallback_count + sum(natural_counts)
    text = reg.render()
    # the +Inf bucket of the fallback series must equal its sample count
    inf_line = [ln for ln in text.splitlines()
                if 'lat_bucket{' in ln and f'route="{LABEL_OVERFLOW}"' in ln and 'le="+Inf"' in ln]
    print(f"\n[hist-cap] cap=3 input={n} samples natural_counts={natural_counts} "
          f"fallback_count={fallback_count} total={total_count}")
    print("  rule=histogram count conserved; fallback +Inf bucket equals its count")
    assert total_count == n
    assert reg.family_series_count("lat") == 3
    assert inf_line and inf_line[0].endswith(f" {fallback_count:g}")


def test_inflight_gauge_fallback_pairs_stay_balanced() -> None:
    # Note: the HTTP in-flight family is intentionally uncapped (its +1/-1
    # pair must always resolve to the same key); this verifies that *if* a
    # gauge family is capped, perfectly-paired increments still cancel.
    reg = MetricsRegistry()
    reg.register_gauge("inflight", "in-flight", ("route",), max_series=2)
    # many distinct routes each do +1 then -1 with a STABLE key, so every
    # pair hits the same admission decision
    for i in range(100):
        route = f"/r{i}"
        reg.inc_gauge("inflight", (("route", route),), 1.0)
        reg.inc_gauge("inflight", (("route", route),), -1.0)
    natural = reg.gauge_value("inflight", (("route", "/r0"),))
    fallback = reg.gauge_value("inflight", (("route", LABEL_OVERFLOW),))
    print(f"\n[gauge-balance] cap=2 input=100 distinct +1/-1 pairs (stable keys) "
          f"natural={natural:g} fallback={fallback:g}")
    print("  rule=each inc/dec pair shares one key -> all gauges back at 0")
    assert reg.family_series_count("inflight") == 2
    assert natural == 0.0
    assert fallback == 0.0


def test_http_inflight_family_is_uncapped_by_design() -> None:
    # The standard HTTP registry leaves the in-flight gauge uncapped so the
    # probe/post-route key difference and the self-observed /metrics scrape
    # can never strand value in a fallback series.
    reg = create_http_registry(max_series=7)
    assert reg.family_max_series(HTTP_INFLIGHT) is None
    print(f"\n[inflight-uncapped] cap passed=7; "
          f"http_inflight_requests cap={reg.family_max_series(HTTP_INFLIGHT)} (None)")
    assert reg.family_max_series(HTTP_REQUESTS_TOTAL) == 7
    assert reg.family_max_series(HTTP_REQUEST_DURATION_SECONDS) == 7


# --------------------------------------------------------------------------- #
# concurrency: cap cannot be breached, totals conserved
# --------------------------------------------------------------------------- #
def test_cap_holds_under_concurrency() -> None:
    cap = 20
    reg = _make_capped_registry(cap)
    n_threads, per_thread = 16, 100

    def worker(tid: int) -> None:
        for j in range(per_thread):
            # every thread invents fresh keys -> 1600 distinct combinations
            reg.inc_counter(
                "reqs_total",
                (("route", f"/t{tid}/r{j}"), ("status", str(200 + (j % 5)))),
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    total = n_threads * per_thread
    count = reg.family_series_count("reqs_total")
    fallback = reg.counter_value(
        "reqs_total", (("route", LABEL_OVERFLOW), ("status", LABEL_OVERFLOW))
    )
    # sum *every* stored series to prove conservation
    text = reg.render()
    stored_sum = 0.0
    for line in text.splitlines():
        if line.startswith("reqs_total{"):
            stored_sum += float(line.rsplit(" ", 1)[1])
    print(f"\n[cap-concurrency] cap={cap} threads={n_threads} writes/thread={per_thread} "
          f"total_writes={total}")
    print(f"  rule=series_count<={cap} (actual {count}); stored_sum=={total} "
          f"(actual {stored_sum:g}); fallback={fallback:g}")
    assert count <= cap
    assert stored_sum == total
    assert fallback == total - (cap - 1)


# --------------------------------------------------------------------------- #
# unlimited / validation / global switch
# --------------------------------------------------------------------------- #
def test_none_cap_means_unlimited() -> None:
    reg = _make_capped_registry(None)
    for i in range(2000):
        reg.inc_counter("reqs_total", (("route", f"/r{i}"), ("status", "200")))
    count = reg.family_series_count("reqs_total")
    active = reg.family_is_overflowing("reqs_total")
    print(f"\n[unlimited] max_series=None input=2000 distinct "
          f"rule=no fallback ever; series_count={count} active={active}")
    assert count == 2000
    assert active is False
    assert reg.family_max_series("reqs_total") is None


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10"])
def test_invalid_max_series_rejected(bad: object) -> None:
    reg = MetricsRegistry()
    print(f"\n[cap-validate] input max_series={bad!r} rule=positive int or None")
    with pytest.raises(ValueError):
        reg.register_counter("x", "x", ("a",), max_series=bad)  # type: ignore[arg-type]


def test_global_switch_hides_everything_and_drops_writes() -> None:
    reg = create_http_registry(max_series=10)
    reg.inc_counter(
        HTTP_REQUESTS_TOTAL,
        (("method", "GET"), ("route", "/"), ("status", "200"),
         ("outcome", "ok"), ("result", "success")),
    )
    reg.set_pipeline_enabled(False)
    print("\n[switch-off] rule=disabled pipeline renders empty incl. self-monitoring")
    assert reg.pipeline_enabled() is False
    assert reg.render() == ""
    # writes while off are no-ops and cannot create series
    reg.inc_counter(
        HTTP_REQUESTS_TOTAL,
        (("method", "GET"), ("route", "/off"), ("status", "200"),
         ("outcome", "ok"), ("result", "success")),
    )
    reg.set_pipeline_enabled(True)
    text = reg.render()
    assert "observability_" in text  # back on -> self-monitoring visible again
    assert 'route="/off"' not in text  # write during off-window was not backfilled
    print("  after re-enable: self-monitoring visible; off-window write absent")


# --------------------------------------------------------------------------- #
# self-monitoring exposition
# --------------------------------------------------------------------------- #
def test_self_monitoring_reports_cap_state_and_overflow_count() -> None:
    reg = create_http_registry(max_series=4)
    # drive only the request counter past its budget
    for i in range(20):
        reg.inc_counter(
            HTTP_REQUESTS_TOTAL,
            (("method", "GET"), ("route", f"/r{i}"), ("status", "200"),
             ("outcome", "ok"), ("result", "success")),
        )
    text = reg.render()
    print(f"\n[self] input=20 distinct request series, cap=4")
    wanted = {}
    for line in text.splitlines():
        if line.startswith(("observability_",)):
            print("  " + line)
            wanted.setdefault(line.split("{")[0].split(" ")[0], []).append(line)

    def gauge(metric: str, family: str) -> float:
        for line in wanted.get(metric, []):
            if f'family="{family}"' in line:
                return float(line.rsplit(" ", 1)[1])
        raise AssertionError(f"{metric}{{family={family}}} missing")

    assert gauge(OBS_SERIES, HTTP_REQUESTS_TOTAL) == 4
    assert gauge(OBS_SERIES_CAP, HTTP_REQUESTS_TOTAL) == 4
    assert gauge(OBS_OVERFLOW_ACTIVE, HTTP_REQUESTS_TOTAL) == 1
    assert gauge(OBS_OVERFLOW_TOTAL, HTTP_REQUESTS_TOTAL) == 17  # 20 writes - 3 natural
    # families still under budget report active=0
    assert gauge(OBS_OVERFLOW_ACTIVE, HTTP_INFLIGHT) == 0
    assert gauge(OBS_SERIES, HTTP_INFLIGHT) == 0
    # unlimited cap would render as 0; here it renders the configured value
    assert gauge(OBS_SERIES_CAP, HTTP_REQUEST_DURATION_SECONDS) == 4


def test_self_monitoring_render_duration_is_recorded() -> None:
    reg = create_http_registry()
    reg.render()
    reg.render()
    count, _sum = reg.histogram_stats(OBS_RENDER_SECONDS, ())
    print(f"\n[self-render] rule=each render records one duration sample; count={count:g}")
    assert count == 2


def test_self_monitoring_is_bounded_and_follows_switch() -> None:
    reg = create_http_registry(max_series=50)
    for i in range(300):
        reg.inc_counter(
            HTTP_REQUESTS_TOTAL,
            (("method", "GET"), ("route", f"/r{i}"), ("status", "200"),
             ("outcome", "ok"), ("result", "success")),
        )
    text = reg.render()
    # overflow counter family: one row per *user* family, never per request
    overflow_rows = [ln for ln in text.splitlines() if ln.startswith(f"{OBS_OVERFLOW_TOTAL}{{")]
    self_series = [ln for ln in text.splitlines() if ln.startswith("observability_")]
    print(f"\n[self-bounded] overflow_total rows={len(overflow_rows)} "
          f"(expect <= 3 user families); total observability lines={len(self_series)}")
    for ln in overflow_rows:
        print("  " + ln)
    assert 1 <= len(overflow_rows) <= 3
    # self families must not themselves be capped into an overflow fallback
    assert all(LABEL_OVERFLOW not in ln for ln in self_series)
    # and they vanish with the global switch
    reg.set_pipeline_enabled(False)
    assert "observability_" not in reg.render()


def test_default_cap_is_present_and_finite() -> None:
    reg = create_http_registry()
    assert DEFAULT_MAX_SERIES == 500
    assert reg.family_max_series(HTTP_REQUESTS_TOTAL) == DEFAULT_MAX_SERIES
    print(f"\n[default-cap] DEFAULT_MAX_SERIES={DEFAULT_MAX_SERIES}")
