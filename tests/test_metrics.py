"""Unit tests for the metric primitives.

Covers concurrency-safe counters, histogram bucket/count conservation,
gauge balance, bounded label sets, text escaping and duplicate registration.
"""

from __future__ import annotations

import threading

import pytest

from app.observability.metrics import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    Registry,
    escape_label_value,
    format_number,
)
from conftest import counter_total, logger, parse_samples


def test_counter_concurrent_increments_are_not_lost_or_duplicated() -> None:
    """Input: 32 threads x 1000 increments on one series.

    Verdict basis: the final counter must equal the exact number of
    increments (32000); any lost update lowers it, any double increment
    raises it.
    """
    registry = Registry()
    counter = Counter(
        "http_requests_total", "h", ["method", "route"], [["GET", "/r"]]
    )
    registry.register(counter)

    threads_count = 32
    per_thread = 1000
    logger.info(
        "INPUT counter concurrency: threads=%s increments/thread=%s",
        threads_count, per_thread,
    )

    def worker() -> None:
        for _ in range(per_thread):
            counter.inc(["GET", "/r"])

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    expected = threads_count * per_thread
    actual = counter.value(["GET", "/r"])
    logger.info("VERDICT expected=%s actual=%s", expected, actual)
    assert actual == expected


def test_counter_is_bounded_to_declared_label_combinations() -> None:
    """Input: an increment with a route value never declared.

    Verdict basis: it must raise ValueError; otherwise arbitrary paths
    could create unbounded time series.
    """
    registry = Registry()
    counter = Counter("c", "h", ["route"], [["/declared"]])
    registry.register(counter)
    logger.info("INPUT unknown label route=/attacker/12345")
    with pytest.raises(ValueError, match="unregistered label combination"):
        counter.inc(["/attacker/12345"])
    logger.info("VERDICT ValueError raised; declared value untouched=%s",
                counter.value(["/declared"]))
    assert counter.value(["/declared"]) == 0.0


def test_histogram_buckets_and_conservation() -> None:
    """Input: observations [0.01, 0.2, 0.9, 3.0].

    Verdict basis: the +Inf bucket count and _count equal the observation
    count (4), _sum equals the arithmetic sum, and each finite cumulative
    bucket counts exactly the observations at or below its boundary.
    """
    registry = Registry()
    hist = Histogram(
        "http_request_duration_seconds", "h", ["route"], [["/r"]],
        buckets=(0.1, 0.5, 1.0, 5.0),
    )
    registry.register(hist)
    observations = [0.01, 0.2, 0.9, 3.0]
    logger.info("INPUT observations=%s buckets=(0.1,0.5,1.0,5.0)", observations)
    for value in observations:
        hist.observe(["/r"], value)

    counts = hist.bucket_counts(["/r"])
    # <=0.1: {0.01}=1 ; <=0.5: 2 ; <=1.0: 3 ; <=5.0: 4 ; +Inf: 4
    expected_counts = (1, 2, 3, 4, 4)
    logger.info("VERDICT bucket counts=%s expected=%s count=%s sum=%s",
                counts, expected_counts, hist.count(["/r"]), hist.sum(["/r"]))
    assert counts == expected_counts
    assert hist.count(["/r"]) == len(observations)
    assert hist.sum(["/r"]) == pytest.approx(sum(observations))
    # Rendered exposition must satisfy the same conservation identity.
    rendered = registry.render()
    series = parse_samples(rendered)
    rendered_count = series[
        "http_request_duration_seconds_count{route=\"/r\"}"
    ]
    rendered_inf = series[
        'http_request_duration_seconds_bucket{route="/r",le="+Inf"}'
    ]
    logger.info("VERDICT rendered count=%s +Inf bucket=%s", rendered_count, rendered_inf)
    assert rendered_count == rendered_inf == len(observations)


def test_gauge_inc_dec_returns_to_zero() -> None:
    """Input: 25 threads doing 50 (inc, dec) pairs each.

    Verdict basis: every inc is paired with a dec, so the gauge must be 0;
    a missed operation would leave it non-zero.
    """
    registry = Registry()
    gauge = Gauge("http_requests_in_progress", "h", ["route"], [["/r"]])
    registry.register(gauge)
    threads_count, pairs = 25, 50
    logger.info("INPUT gauge threads=%s pairs/thread=%s", threads_count, pairs)

    def worker() -> None:
        for _ in range(pairs):
            gauge.inc(["/r"])
            gauge.dec(["/r"])

    threads = [threading.Thread(target=worker) for _ in range(threads_count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    logger.info("VERDICT gauge final=%s", gauge.value(["/r"]))
    assert gauge.value(["/r"]) == 0.0


def test_registry_rejects_duplicate_registration_and_renders_format() -> None:
    """Inputs: two metrics sharing a name; a scrape of both.

    Verdict bases: duplicate registration raises ValueError (no duplicated
    series), headers declare HELP/TYPE, values use Prometheus number
    formatting, and the content type is the standard text exposition type.
    """
    registry = Registry()
    registry.register(Counter("c_total", "first", [], [[]]))
    logger.info("INPUT register second counter with same name c_total")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(Counter("c_total", "second", [], [[]]))

    registry.register(Gauge("g", "gauge help", ["r"], [["x"]]))
    text = registry.render()
    logger.info("VERDICT rendered text:\n%s", text)
    assert "# HELP c_total first" in text
    assert "# TYPE c_total counter" in text
    assert "# TYPE g gauge" in text
    assert registry.content_type == CONTENT_TYPE_LATEST
    assert text.endswith("\n")


def test_disabled_or_never_touched_series_absent_from_output() -> None:
    """Input: a registry with one metric whose values are all zero.

    Verdict basis: zero combinations produce HELP/TYPE headers but no
    sample lines; a completely empty registry renders an empty body.
    """
    registry = Registry()
    registry.register(Counter("idle_total", "never used", ["route"], [["/r"]]))
    text = registry.render()
    logger.info("VERDICT idle registry text=%r", text)
    assert "idle_total{" not in text
    assert "# TYPE idle_total counter" in text
    assert Registry().render() == ""


def test_label_value_escaping_and_number_formatting() -> None:
    """Inputs: hostile label value; special float values.

    Verdict basis: backslash/newline/quote are escaped per spec, and NaN /
    infinities use the Prometheus spellings so output stays parseable.
    """
    raw = 'a"b\\c\nd'
    logger.info("INPUT escaping raw=%r", raw)
    assert escape_label_value(raw) == 'a\\"b\\\\c\\nd'
    logger.info("VERDICT escaped=%r", escape_label_value(raw))
    assert format_number(float("nan")) == "NaN"
    assert format_number(float("inf")) == "+Inf"
    assert format_number(float("-inf")) == "-Inf"


def test_histogram_rejects_non_monotonic_buckets() -> None:
    """Input: bucket boundaries out of order.

    Verdict basis: construction must fail, otherwise cumulative buckets
    would be meaningless.
    """
    logger.info("INPUT buckets=(0.5,0.1)")
    with pytest.raises(ValueError, match="strictly increasing"):
        Histogram("h", "h", [], [[]], buckets=(0.5, 0.1))
    logger.info("VERDICT ValueError raised")


def test_total_samples_helpers_consistency() -> None:
    """Input: one counter with two non-zero series.

    Verdict basis: counter_total sums every series of the metric and
    ignores headers.
    """
    registry = Registry()
    counter = Counter("c", "h", ["route"], [["/a"], ["/b"]])
    registry.register(counter)
    counter.inc(["/a"], 3)
    counter.inc(["/b"], 2)
    samples = parse_samples(registry.render())
    total = counter_total(samples, "c")
    logger.info("VERDICT per-series total=%s", total)
    assert total == 5.0
