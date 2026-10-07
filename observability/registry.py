"""Minimal, concurrency-safe Prometheus text-format metrics registry.

Design notes
------------
* A single :class:`threading.Lock` guards every mutation and every render, so
  concurrent requests can never lose or duplicate a count.  All hot-path
  operations are O(1) dict lookups plus O(#labels) key building: the cost of
  recording a request does not grow with the number of historical requests.
* Series are keyed by the tuple of label *values* in declared label order.
* **Cardinality cap.** Every family may declare ``max_series`` (per family):
  the total number of exposed series of that family can never exceed it,
  including under concurrency.  One slot is reserved up front for a single
  fallback series whose every label position is ``__overflow__``; once the
  natural series budget (``max_series - 1`` distinct keys) is exhausted, every
  new combination is recorded into that one fallback series, so totals and
  histogram buckets stay conserved (nothing is dropped or double counted).
  Redirected writes are themselves counted in a self-monitoring counter.
* A metric family can be disabled, and the whole registry has a global
  on/off switch as well; disabled writes are no-ops and disabled families
  (or a disabled registry) are omitted from :meth:`render`.  Self-monitoring
  lives behind the same switch, so disabling metrics hides it too.
* Registration is idempotent: re-registering an identical family is a no-op,
  which makes repeated application setup safe.
"""
from __future__ import annotations

import bisect
import math
import time
from dataclasses import dataclass
from threading import Lock
from typing import Iterable

from .labels import LABEL_OVERFLOW

LabelTuple = tuple[tuple[str, str], ...]

_COUNTER = "counter"
_GAUGE = "gauge"
_HISTOGRAM = "histogram"


@dataclass(frozen=True)
class _SelfMonitoring:
    """Names of the self-monitoring families maintained during render."""

    series: str
    cap: str
    overflow_active: str
    overflow_total: str
    render_duration: str


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
        # name -> {"help","labelnames","enabled","type",
        #          "max_series": int|None, "self": bool}
        self._meta: dict[str, dict] = {}
        # name -> {labels_key: float}
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._gauges: dict[str, dict[tuple[str, ...], float]] = {}
        # name -> {"buckets": tuple, "series": {labels_key: [counts..., sum]}}
        self._histograms: dict[str, dict] = {}
        # global kill switch: False makes every write a no-op and render empty
        self._enabled: bool = True
        # self-monitoring wiring, if installed
        self._self_monitoring: _SelfMonitoring | None = None

    # ------------------------------------------------------------------ #
    # registration (idempotent)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_max_series(max_series: int | None) -> int | None:
        if max_series is None:
            return None
        if not isinstance(max_series, int) or isinstance(max_series, bool) or max_series < 1:
            raise ValueError("max_series must be a positive integer or None (unlimited)")
        return max_series

    def _register(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        mtype: str,
        enabled: bool,
        max_series: int | None = None,
        *,
        self_family: bool = False,
    ) -> None:
        max_series = self._validate_max_series(max_series)
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
                "max_series": max_series,
                "self": self_family,
            }
            if mtype == _COUNTER:
                self._counters[name] = {}
            elif mtype == _GAUGE:
                self._gauges[name] = {}
            else:
                self._histograms[name] = {"buckets": (), "series": {}}

    def register_counter(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        *,
        enabled: bool = True,
        max_series: int | None = None,
    ) -> None:
        self._register(name, help_text, labelnames, _COUNTER, enabled, max_series)

    def register_gauge(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        *,
        enabled: bool = True,
        max_series: int | None = None,
    ) -> None:
        self._register(name, help_text, labelnames, _GAUGE, enabled, max_series)

    def register_histogram(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        buckets: tuple[float, ...],
        *,
        enabled: bool = True,
        max_series: int | None = None,
    ) -> None:
        ordered = tuple(sorted(float(b) for b in buckets))
        self._register(name, help_text, labelnames, _HISTOGRAM, enabled, max_series)
        with self._lock:
            self._histograms[name]["buckets"] = ordered

    # ------------------------------------------------------------------ #
    # self-monitoring wiring
    # ------------------------------------------------------------------ #
    def register_self_monitoring(
        self,
        *,
        series_name: str,
        cap_name: str,
        overflow_active_name: str,
        overflow_total_name: str,
        render_duration_name: str,
        render_buckets: tuple[float, ...],
    ) -> _SelfMonitoring:
        """Register the five bounded self-monitoring families.

        All five are marked as self families: they are never cardinality
        capped themselves, and their values are refreshed by :meth:`render`.
        Self-monitoring follows the registry's global enable switch, so a
        disabled registry renders none of these families.
        """
        one_label = ("family",)
        self.register_gauge(
            series_name, "Number of currently exposed series per metric family.", one_label
        )
        self.register_gauge(
            cap_name, "Configured per-family series cap (0 means unlimited).", one_label
        )
        self.register_gauge(
            overflow_active_name,
            "1 while the family is redirecting new combinations to the fallback series.",
            one_label,
        )
        self.register_counter(
            overflow_total_name,
            "Number of writes redirected to the cardinality-cap fallback series.",
            one_label,
        )
        self._register(
            render_duration_name,
            "Wall-clock time spent rendering one /metrics scrape in seconds.",
            (),
            _HISTOGRAM,
            True,
        )
        with self._lock:
            self._histograms[render_duration_name]["buckets"] = tuple(
                sorted(float(b) for b in render_buckets)
            )
            wiring = _SelfMonitoring(
                series=series_name,
                cap=cap_name,
                overflow_active=overflow_active_name,
                overflow_total=overflow_total_name,
                render_duration=render_duration_name,
            )
            for nm in (
                series_name,
                cap_name,
                overflow_active_name,
                overflow_total_name,
                render_duration_name,
            ):
                self._meta[nm]["self"] = True
            self._self_monitoring = wiring
            return wiring

    # ------------------------------------------------------------------ #
    # enable / disable (global switch and per-family switch)
    # ------------------------------------------------------------------ #
    def set_pipeline_enabled(self, enabled: bool) -> None:
        """Flip the global kill switch.

        When disabled every write is a no-op and :meth:`render` emits nothing
        -- including the self-monitoring families, which can never leak out
        while the metrics pipeline is off.
        """
        with self._lock:
            self._enabled = bool(enabled)

    def pipeline_enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if name in self._meta:
                self._meta[name]["enabled"] = enabled

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            meta = self._meta.get(name)
            return bool(meta and meta["enabled"])

    # ------------------------------------------------------------------ #
    # cardinality cap (hot path)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _overflow_key(labelnames: tuple[str, ...]) -> tuple[str, ...]:
        """The single reserved fallback key: ``__overflow__`` in every slot."""
        return (LABEL_OVERFLOW,) * len(labelnames)

    def _admit(
        self,
        store: dict[tuple[str, ...], object],
        meta: dict,
        key: tuple[str, ...],
    ) -> tuple[tuple[str, ...], bool]:
        """Return ``(storage_key, redirected)`` for an incoming write.

        Invariant: for a capped family the store never holds more than
        ``max_series`` keys.  One slot is permanently reserved for the
        overflow fallback, so at most ``max_series - 1`` distinct natural
        keys are admitted; the fallback series is created lazily on the
        first overflow.  Called with ``self._lock`` held.
        """
        cap = meta["max_series"]
        if cap is None or meta.get("self"):
            return key, False
        if key in store:
            return key, False
        overflow = self._overflow_key(meta["labelnames"])
        # Natural budget exhausted -> every unseen combination funnels into
        # the one reserved fallback series.  Count natural keys rather than
        # the store size, since the lazily-created fallback must not eat
        # into the natural budget.
        natural = len(store) - (1 if overflow in store else 0)
        if natural >= cap - 1:
            return overflow, True
        return key, False

    def _count_overflow(self, family: str) -> None:
        """Increment the self-monitoring overflow counter for ``family``.

        Called with ``self._lock`` held and only when a redirect happened.
        Missing self-monitoring wiring simply means the count is skipped.
        """
        wiring = self._self_monitoring
        if wiring is None:
            return
        store = self._counters.get(wiring.overflow_total)
        if store is None:
            return
        key = (family,)
        store[key] = store.get(key, 0.0) + 1.0

    # ------------------------------------------------------------------ #
    # write path (hot path: O(1) per call)
    # ------------------------------------------------------------------ #
    def inc_counter(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        with self._lock:
            if not self._enabled:
                return
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _COUNTER:
                return
            key = _labels_key(meta["labelnames"], labels)
            series = self._counters[name]
            key, redirected = self._admit(series, meta, key)
            series[key] = series.get(key, 0.0) + value
            if redirected:
                self._count_overflow(name)

    def inc_gauge(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        with self._lock:
            if not self._enabled:
                return
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _GAUGE:
                return
            key = _labels_key(meta["labelnames"], labels)
            series = self._gauges[name]
            key, redirected = self._admit(series, meta, key)
            series[key] = series.get(key, 0.0) + value
            if redirected:
                self._count_overflow(name)

    def set_gauge(self, name: str, labels: LabelTuple, value: float) -> None:
        """Set (rather than increment) one gauge series; honors cap/switch."""
        with self._lock:
            if not self._enabled:
                return
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _GAUGE:
                return
            key = _labels_key(meta["labelnames"], labels)
            series = self._gauges[name]
            key, _redirected = self._admit(series, meta, key)
            series[key] = float(value)

    def observe(self, name: str, labels: LabelTuple, value: float) -> None:
        with self._lock:
            if not self._enabled:
                return
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _HISTOGRAM:
                return
            hist = self._histograms[name]
            key = _labels_key(meta["labelnames"], labels)
            series = hist["series"]
            key, redirected = self._admit(series, meta, key)
            self._hist_observe(hist, key, value)
            if redirected:
                self._count_overflow(name)

    @staticmethod
    def _hist_observe(hist: dict, key: tuple[str, ...], value: float) -> None:
        """Add one sample to a histogram series (lock held by caller)."""
        buckets: tuple[float, ...] = hist["buckets"]
        series = hist["series"]
        # data layout: [count_le_b0, ..., count_le_bN, count_le_+Inf, sum]
        data = series.get(key)
        if data is None:
            data = [0.0] * (len(buckets) + 2)
            series[key] = data
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

    def family_series_count(self, name: str) -> int:
        """Number of series currently stored for one family (0 if unknown)."""
        with self._lock:
            meta = self._meta.get(name)
            if meta is None:
                return 0
            mtype = meta["type"]
            if mtype == _COUNTER:
                return len(self._counters[name])
            if mtype == _GAUGE:
                return len(self._gauges[name])
            return len(self._histograms[name]["series"])

    def family_max_series(self, name: str) -> int | None:
        """Configured cap for one family (``None`` means unlimited)."""
        with self._lock:
            meta = self._meta.get(name)
            return None if meta is None else meta["max_series"]

    def family_is_overflowing(self, name: str) -> bool:
        """Whether the fallback series of one family currently exists."""
        with self._lock:
            meta = self._meta.get(name)
            if meta is None:
                return False
            overflow = self._overflow_key(meta["labelnames"])
            mtype = meta["type"]
            if mtype == _COUNTER:
                return overflow in self._counters[name]
            if mtype == _GAUGE:
                return overflow in self._gauges[name]
            return overflow in self._histograms[name]["series"]

    # ------------------------------------------------------------------ #
    # exposition
    # ------------------------------------------------------------------ #
    def render(self) -> str:
        """Render enabled families in Prometheus text exposition format.

        A disabled registry renders the empty string and updates nothing.
        Otherwise the bounded self-monitoring families are refreshed first
        (current series counts, configured caps, overflow-active flags) and
        the wall-clock duration of the whole render is recorded into the
        self-monitoring render-duration histogram, so the scrape that reads
        the state also reports the cost of producing it.
        """
        started = time.perf_counter()
        with self._lock:
            if not self._enabled:
                return ""
            self._refresh_self_gauges()
            lines: list[str] = []
            for name in sorted(self._meta):
                meta = self._meta[name]
                if not meta["enabled"]:
                    continue
                lines.extend(self._render_family(name, meta))
            wiring = self._self_monitoring
            if wiring is not None:
                self._hist_observe(
                    self._histograms[wiring.render_duration], (), time.perf_counter() - started
                )
            return "\n".join(lines) + ("\n" if lines else "")

    def _refresh_self_gauges(self) -> None:
        """Recompute series/cap/overflow-active gauges for user families."""
        wiring = self._self_monitoring
        if wiring is None:
            return

        def family_store(name: str):
            mtype = self._meta[name]["type"]
            if mtype == _COUNTER:
                return self._counters[name]
            if mtype == _GAUGE:
                return self._gauges[name]
            return self._histograms[name]["series"]

        for name, meta in self._meta.items():
            if meta.get("self"):
                # Self families describe the others, not themselves.
                continue
            if not meta["enabled"]:
                # Disabled families expose nothing; drop any stale rows from
                # a previous enabled period so the gauges describe only the
                # currently live exposition surface.
                self._gauges[wiring.series].pop((name,), None)
                self._gauges[wiring.cap].pop((name,), None)
                self._gauges[wiring.overflow_active].pop((name,), None)
                continue
            store = family_store(name)
            self._gauges[wiring.series][(name,)] = float(len(store))
            cap = meta["max_series"]
            self._gauges[wiring.cap][(name,)] = float(0 if cap is None else cap)
            overflow = self._overflow_key(meta["labelnames"])
            self._gauges[wiring.overflow_active][(name,)] = 1.0 if overflow in store else 0.0

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
