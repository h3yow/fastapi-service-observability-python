"""Minimal, concurrency-safe Prometheus text-format metrics registry.

Design notes
------------
* A single :class:`threading.Lock` guards every mutation and every render, so
  concurrent requests can never lose or duplicate a count.  All hot-path
  operations are O(1) dict lookups plus O(#labels) key building: the cost of
  recording a request does not grow with the number of historical requests.
* Series are keyed by the tuple of label *values* in declared label order, so
  the number of stored series is bounded by the (bounded) label vocabulary in
  :mod:`observability.labels`.
* A metric family can be disabled; disabled families accept no-ops on the
  write path and are omitted from :meth:`render`.
* Registration is idempotent: re-registering an identical family is a no-op,
  which makes repeated application setup safe.
"""
from __future__ import annotations

import bisect
import math
from threading import Lock
from typing import Iterable

LabelTuple = tuple[tuple[str, str], ...]

_COUNTER = "counter"
_GAUGE = "gauge"
_HISTOGRAM = "histogram"


def _labels_key(labelnames: tuple[str, ...], labels: LabelTuple) -> tuple[str, ...]:
    """Project ``labels`` onto the declared label names, in declared order."""
    provided = dict(labels)
    return tuple(provided.get(name, "") for name in labelnames)


def _escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_number(value: float) -> str:
    if isinstance(value, float) and math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value):
        return str(int(value))
    return repr(value)


class MetricsRegistry:
    """A small registry for counters, gauges and fixed-bucket histograms."""

    def __init__(self) -> None:
        self._lock: Lock = Lock()
        # name -> {"help": str, "labelnames": tuple, "enabled": bool}
        self._meta: dict[str, dict] = {}
        # name -> {labels_key: float}
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._gauges: dict[str, dict[tuple[str, ...], float]] = {}
        # name -> {"buckets": tuple, "series": {labels_key: [counts..., sum]}}
        self._histograms: dict[str, dict] = {}

    # ------------------------------------------------------------------ #
    # registration (idempotent)
    # ------------------------------------------------------------------ #
    def _register(self, name: str, help_text: str, labelnames: tuple[str, ...], mtype: str, enabled: bool) -> None:
        with self._lock:
            meta = self._meta.get(name)
            if meta is not None:
                if meta["type"] != mtype or meta["labelnames"] != labelnames:
                    raise ValueError(f"metric {name!r} already registered with a different spec")
                meta["enabled"] = enabled
                return
            self._meta[name] = {
                "help": help_text,
                "labelnames": labelnames,
                "type": mtype,
                "enabled": enabled,
            }
            if mtype == _COUNTER:
                self._counters[name] = {}
            elif mtype == _GAUGE:
                self._gauges[name] = {}
            else:
                self._histograms[name] = {"buckets": (), "series": {}}

    def register_counter(self, name: str, help_text: str, labelnames: tuple[str, ...], *, enabled: bool = True) -> None:
        self._register(name, help_text, labelnames, _COUNTER, enabled)

    def register_gauge(self, name: str, help_text: str, labelnames: tuple[str, ...], *, enabled: bool = True) -> None:
        self._register(name, help_text, labelnames, _GAUGE, enabled)

    def register_histogram(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        buckets: tuple[float, ...],
        *,
        enabled: bool = True,
    ) -> None:
        ordered = tuple(sorted(float(b) for b in buckets))
        self._register(name, help_text, labelnames, _HISTOGRAM, enabled)
        with self._lock:
            self._histograms[name]["buckets"] = ordered

    # ------------------------------------------------------------------ #
    # enable / disable
    # ------------------------------------------------------------------ #
    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if name in self._meta:
                self._meta[name]["enabled"] = enabled

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            meta = self._meta.get(name)
            return bool(meta and meta["enabled"])

    # ------------------------------------------------------------------ #
    # write path (hot path: O(1) per call)
    # ------------------------------------------------------------------ #
    def inc_counter(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _COUNTER:
                return
            key = _labels_key(meta["labelnames"], labels)
            series = self._counters[name]
            series[key] = series.get(key, 0.0) + value

    def inc_gauge(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _GAUGE:
                return
            key = _labels_key(meta["labelnames"], labels)
            series = self._gauges[name]
            series[key] = series.get(key, 0.0) + value

    def observe(self, name: str, labels: LabelTuple, value: float) -> None:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _HISTOGRAM:
                return
            hist = self._histograms[name]
            buckets: tuple[float, ...] = hist["buckets"]
            key = _labels_key(meta["labelnames"], labels)
            # data layout: [count_le_b0, ..., count_le_bN, count_le_+Inf, sum]
            data = hist["series"].get(key)
            if data is None:
                data = [0.0] * (len(buckets) + 2)
                hist["series"][key] = data
            idx = bisect.bisect_left(buckets, value)
            for i in range(idx, len(buckets) + 1):
                data[i] += 1.0
            data[-1] += value

    # ------------------------------------------------------------------ #
    # read path (used by tests and debugging)
    # ------------------------------------------------------------------ #
    def counter_value(self, name: str, labels: LabelTuple) -> float:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or meta["type"] != _COUNTER:
                return 0.0
            return self._counters[name].get(_labels_key(meta["labelnames"], labels), 0.0)

    def gauge_value(self, name: str, labels: LabelTuple) -> float:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or meta["type"] != _GAUGE:
                return 0.0
            return self._gauges[name].get(_labels_key(meta["labelnames"], labels), 0.0)

    def histogram_stats(self, name: str, labels: LabelTuple) -> tuple[float, float]:
        """Return ``(count, sum)`` for one histogram series."""
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or meta["type"] != _HISTOGRAM:
                return 0.0, 0.0
            data = self._histograms[name]["series"].get(_labels_key(meta["labelnames"], labels))
            if data is None:
                return 0.0, 0.0
            return data[-2], data[-1]

    # ------------------------------------------------------------------ #
    # exposition
    # ------------------------------------------------------------------ #
    def render(self) -> str:
        """Render enabled families in Prometheus text exposition format."""
        with self._lock:
            lines: list[str] = []
            for name in sorted(self._meta):
                meta = self._meta[name]
                if not meta["enabled"]:
                    continue
                lines.extend(self._render_family(name, meta))
            return "\n".join(lines) + ("\n" if lines else "")

    def _render_family(self, name: str, meta: dict) -> Iterable[str]:
        labelnames: tuple[str, ...] = meta["labelnames"]
        mtype: str = meta["type"]
        yield f"# HELP {name} {meta['help']}"
        yield f"# TYPE {name} {mtype}"

        def label_str(key: tuple[str, ...], extra: tuple[tuple[str, str], ...] = ()) -> str:
            pairs = list(zip(labelnames, key)) + list(extra)
            if not pairs:
                return ""
            inner = ",".join(f'{k}="{_escape_label_value(v)}"' for k, v in pairs)
            return "{" + inner + "}"

        if mtype == _COUNTER:
            for key in sorted(self._counters[name]):
                yield f"{name}{label_str(key)} {_format_number(self._counters[name][key])}"
        elif mtype == _GAUGE:
            for key in sorted(self._gauges[name]):
                yield f"{name}{label_str(key)} {_format_number(self._gauges[name][key])}"
        else:
            hist = self._histograms[name]
            buckets: tuple[float, ...] = hist["buckets"]
            for key in sorted(hist["series"]):
                data = hist["series"][key]
                for i, bound in enumerate(buckets):
                    yield f'{name}_bucket{label_str(key, (("le", _format_number(bound)),))} {_format_number(data[i])}'
                yield f'{name}_bucket{label_str(key, (("le", "+Inf"),))} {_format_number(data[len(buckets)])}'
                yield f"{name}_sum{label_str(key)} {_format_number(data[-1])}"
                yield f"{name}_count{label_str(key)} {_format_number(data[len(buckets)])}"
