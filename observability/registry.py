"""Minimal, concurrency-safe Prometheus text-format metrics registry.

Design notes
------------
* A single :class:`threading.Lock` guards every mutation and every render, so
  concurrent requests can never lose or duplicate a count.  All hot-path
  operations are O(1) dict lookups plus O(#labels) key building: the cost of
  recording a request does not grow with the number of historical requests.
* Series are keyed by the tuple of label *values* in declared label order.
* **Cardinality cap.**  ``max_series`` bounds the number of distinct series
  (label combinations) exposed across all regular families.  Once the budget
  is spent, recordings with unseen label combinations are folded into a fixed
  per-family *fallback* series whose every label value is ``__overflow__``;
  the number of folded recordings is tracked per family.  One slot per
  regular family is reserved up front for its fallback series, so the
  fallback can always be created without exceeding the cap and no recording
  is ever dropped (conservation holds: folded requests still increment the
  fallback counter/gauge/histogram of their family).  The check on the write
  path is a single dict membership test plus an integer comparison, so
  high-concurrency recording does not get slower when the cap is close.
  ``max_series=None`` disables the cap entirely.
* **Self-monitoring.**  With ``self_monitoring=True`` the registry maintains
  four internal families (``observability_*``) describing the protection
  layer itself: current series per family, the configured limit, folded
  recordings per family, and the wall-clock cost of the last render.
  Internal families are exempt from the cap, are rendered after the regular
  families, and follow the same enable/disable switches, so disabling the
  pipeline hides them too.
* A metric family can be disabled; disabled families accept no-ops on the
  write path and are omitted from :meth:`render`.
* Registration is idempotent: re-registering an identical family is a no-op,
  which makes repeated application setup safe.
"""
from __future__ import annotations

import bisect
import math
import time
from threading import Lock
from typing import Iterable

LabelTuple = tuple[tuple[str, str], ...]

_COUNTER = "counter"
_GAUGE = "gauge"
_HISTOGRAM = "histogram"

#: Label value used for every label position of a family's fallback series.
OVERFLOW_LABEL_VALUE = "__overflow__"

#: Gauge: current number of series per capped family (label ``metric``).
SELF_SERIES_CURRENT = "observability_series_current"
#: Gauge: configured series budget shared by all capped families (+Inf = off).
SELF_SERIES_LIMIT = "observability_series_limit"
#: Counter: recordings folded into the fallback series (label ``metric``).
SELF_OVERFLOW_TOTAL = "observability_overflow_total"
#: Gauge: seconds spent rendering the regular families in the last scrape.
SELF_RENDER_DURATION_SECONDS = "observability_render_duration_seconds"


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

    def __init__(self, *, max_series: int | None = None, self_monitoring: bool = False) -> None:
        self._lock: Lock = Lock()
        #: Shared budget of distinct series across all regular families.
        #: ``None`` (or a non-positive value) means unlimited.
        self._max_series: int | None = max_series if max_series and max_series > 0 else None
        # name -> {"help": str, "labelnames": tuple, "enabled": bool, "internal": bool}
        self._meta: dict[str, dict] = {}
        # name -> {labels_key: float}
        self._counters: dict[str, dict[tuple[str, ...], float]] = {}
        self._gauges: dict[str, dict[tuple[str, ...], float]] = {}
        # name -> {"buckets": tuple, "series": {labels_key: [counts..., sum]}}
        self._histograms: dict[str, dict] = {}
        # --- cardinality-cap state (regular families only) -----------------
        self._series_total: int = 0
        # family name -> number of stored series (fallback included)
        self._family_sizes: dict[str, int] = {}
        # family name -> fallback key (present once the fallback was created)
        self._fallback_keys: dict[str, tuple[str, ...]] = {}
        # family name -> number of recordings folded into the fallback series
        self._overflow_counts: dict[str, float] = {}
        if self_monitoring:
            self._register_self_monitoring()

    # ------------------------------------------------------------------ #
    # registration (idempotent)
    # ------------------------------------------------------------------ #
    def _register(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        mtype: str,
        enabled: bool,
        internal: bool,
    ) -> None:
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
                "internal": internal,
            }
            if mtype == _COUNTER:
                self._counters[name] = {}
            elif mtype == _GAUGE:
                self._gauges[name] = {}
            else:
                self._histograms[name] = {"buckets": (), "series": {}}
            if not internal:
                # One slot of the shared budget is reserved for this family's
                # fallback series until it is actually created (see
                # _resolve_key_locked), so the fallback can never push the
                # exposed series count past the cap.
                self._family_sizes[name] = 0
                self._mirror_series_current(name)
                # Pre-create the family's zero-valued overflow series so the
                # self-monitoring output is stable (absence == exactly zero).
                counter = self._counters.get(SELF_OVERFLOW_TOTAL)
                if counter is not None:
                    counter.setdefault((name,), 0.0)

    def register_counter(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        *,
        enabled: bool = True,
        internal: bool = False,
    ) -> None:
        self._register(name, help_text, labelnames, _COUNTER, enabled, internal)

    def register_gauge(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        *,
        enabled: bool = True,
        internal: bool = False,
    ) -> None:
        self._register(name, help_text, labelnames, _GAUGE, enabled, internal)

    def register_histogram(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        buckets: tuple[float, ...],
        *,
        enabled: bool = True,
        internal: bool = False,
    ) -> None:
        ordered = tuple(sorted(float(b) for b in buckets))
        self._register(name, help_text, labelnames, _HISTOGRAM, enabled, internal)
        with self._lock:
            self._histograms[name]["buckets"] = ordered

    def _register_self_monitoring(self) -> None:
        """Register the internal ``observability_*`` families."""
        self.register_gauge(
            SELF_SERIES_CURRENT,
            "Current number of series per regular metric family, "
            "including the fallback series once it exists.",
            ("metric",),
            internal=True,
        )
        self.register_gauge(
            SELF_SERIES_LIMIT,
            "Configured maximum number of series shared by all regular "
            "metric families (+Inf means unlimited).",
            (),
            internal=True,
        )
        self.register_counter(
            SELF_OVERFLOW_TOTAL,
            "Total recordings folded into a family's fallback series "
            "because the series budget was exhausted.",
            ("metric",),
            internal=True,
        )
        self.register_gauge(
            SELF_RENDER_DURATION_SECONDS,
            "Wall-clock seconds spent rendering the regular metric families "
            "during the most recent scrape.",
            (),
            internal=True,
        )
        with self._lock:
            limit = self._max_series
            self._gauges[SELF_SERIES_LIMIT][()] = math.inf if limit is None else float(limit)

    # ------------------------------------------------------------------ #
    # enable / disable
    # ------------------------------------------------------------------ #
    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if name in self._meta:
                self._meta[name]["enabled"] = enabled

    def set_all_enabled(self, enabled: bool) -> None:
        """Flip every family (regular and internal) at once.

        This is the master switch: disabling it silences the whole pipeline
        including the self-monitoring families, so a disabled registry never
        leaks ``observability_*`` series into the exposition output.
        """
        with self._lock:
            for meta in self._meta.values():
                meta["enabled"] = enabled

    def is_enabled(self, name: str) -> bool:
        with self._lock:
            meta = self._meta.get(name)
            return bool(meta and meta["enabled"])

    # ------------------------------------------------------------------ #
    # cardinality cap (all helpers run under the lock)
    # ------------------------------------------------------------------ #
    def _mirror_series_current(self, name: str) -> None:
        gauge = self._gauges.get(SELF_SERIES_CURRENT)
        if gauge is not None:
            gauge[(name,)] = float(self._family_sizes[name])

    def _note_new_series(self, name: str) -> None:
        self._series_total += 1
        self._family_sizes[name] += 1
        self._mirror_series_current(name)

    def _resolve_key_locked(
        self,
        name: str,
        meta: dict,
        key: tuple[str, ...],
        container: dict,
    ) -> tuple[str, ...]:
        """Return the series key to write to, applying the cardinality cap.

        ``container`` is the family's series dict (used for membership).  A
        key that already exists is always returned unchanged -- the hot path
        is one dict lookup.  A new key is admitted while the shared budget
        (minus the slots reserved for not-yet-created fallback series) allows
        it; otherwise the recording is folded into the family's fallback
        series and accounted for in ``_overflow_counts``.
        """
        if key in container:
            return key
        if meta["internal"] or self._max_series is None:
            if not meta["internal"]:
                self._note_new_series(name)
            return key
        pending_fallbacks = len(self._family_sizes) - len(self._fallback_keys)
        if self._series_total + pending_fallbacks < self._max_series:
            self._note_new_series(name)
            return key
        # Budget exhausted: fold this recording into the fallback series.
        self._overflow_counts[name] = self._overflow_counts.get(name, 0.0) + 1.0
        counter = self._counters.get(SELF_OVERFLOW_TOTAL)
        if counter is not None:
            mkey = (name,)
            counter[mkey] = counter.get(mkey, 0.0) + 1.0
        fallback = self._fallback_keys.get(name)
        if fallback is None:
            fallback = (OVERFLOW_LABEL_VALUE,) * len(meta["labelnames"])
            self._fallback_keys[name] = fallback
            # The reserved slot guarantees this stays within the budget.
            self._note_new_series(name)
        return fallback

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
            key = self._resolve_key_locked(name, meta, key, series)
            series[key] = series.get(key, 0.0) + value

    def inc_gauge(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _GAUGE:
                return
            key = _labels_key(meta["labelnames"], labels)
            series = self._gauges[name]
            key = self._resolve_key_locked(name, meta, key, series)
            series[key] = series.get(key, 0.0) + value

    def observe(self, name: str, labels: LabelTuple, value: float) -> None:
        with self._lock:
            meta = self._meta.get(name)
            if meta is None or not meta["enabled"] or meta["type"] != _HISTOGRAM:
                return
            hist = self._histograms[name]
            buckets: tuple[float, ...] = hist["buckets"]
            key = _labels_key(meta["labelnames"], labels)
            series = hist["series"]
            key = self._resolve_key_locked(name, meta, key, series)
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
    @property
    def max_series(self) -> int | None:
        """The configured series budget (``None`` = unlimited)."""
        return self._max_series

    def series_count(self, name: str | None = None) -> float:
        """Number of stored series, in total or for one regular family."""
        with self._lock:
            if name is None:
                return float(self._series_total)
            return float(self._family_sizes.get(name, 0))

    def overflow_count(self, name: str) -> float:
        """Recordings of family ``name`` folded into its fallback series."""
        with self._lock:
            return self._overflow_counts.get(name, 0.0)

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
        """Render enabled families in Prometheus text exposition format.

        Regular families render first (timed); internal self-monitoring
        families render afterwards, so the render-duration gauge reflects the
        cost of the scrape it is served in.
        """
        with self._lock:
            started = time.perf_counter()
            lines: list[str] = []
            for name in sorted(self._meta):
                meta = self._meta[name]
                if meta["internal"] or not meta["enabled"]:
                    continue
                lines.extend(self._render_family(name, meta))
            elapsed = time.perf_counter() - started
            gauge = self._gauges.get(SELF_RENDER_DURATION_SECONDS)
            if gauge is not None:
                gauge[()] = elapsed
            for name in sorted(self._meta):
                meta = self._meta[name]
                if not meta["internal"] or not meta["enabled"]:
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
