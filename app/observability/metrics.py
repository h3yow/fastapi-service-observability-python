"""Metrics primitives: counter, histogram, gauge, registry and exposition.

All primitives are thread-safe and must be pre-created with the full set of
allowed label-value combinations. Any unknown combination is refused, so
arbitrary request traffic can never introduce new time series at runtime.

Updates are O(1) dictionary lookups into a fixed, pre-allocated structure;
they never scan history and therefore do not slow down as history grows.

The exposition format is Prometheus text (version 0.0.4).
"""

from __future__ import annotations

import math
import re
import threading
from collections.abc import Iterable, Sequence
from typing import Protocol, runtime_checkable

CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"

_METRIC_NAME_RE = re.compile(r"[a-zA-Z_:][a-zA-Z0-9_:]*")
_LABEL_NAME_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]*")

# Default bucket boundaries, in seconds (Prometheus convention).
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.075, 0.1, 0.25, 0.5, 0.75,
    1.0, 2.5, 5.0, 7.5, 10.0,
)


def escape_label_value(value: str) -> str:
    """Escape a label value per the Prometheus text format spec."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_number(value: float) -> str:
    """Format a float the way the Prometheus text format expects."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    return repr(value)


def _validate_name(name: str) -> None:
    if not _METRIC_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid metric name: {name!r}")


def _validate_label_names(labelnames: Sequence[str]) -> None:
    for label in labelnames:
        if not _LABEL_NAME_RE.fullmatch(label):
            raise ValueError(f"invalid label name: {label!r}")
    if len(set(labelnames)) != len(labelnames):
        raise ValueError("duplicate label names are not allowed")


@runtime_checkable
class Metric(Protocol):
    name: str
    documentation: str
    type_name: str
    labelnames: tuple[str, ...]

    def render_lines(self) -> list[str]: ...


class _BaseMetric:
    """Common bookkeeping: name, help text, labels and the bounded key set."""

    type_name = "untyped"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str],
        labelvalues: Iterable[Sequence[str]],
    ) -> None:
        _validate_name(name)
        _validate_label_names(tuple(labelnames))
        self.name = name
        self.documentation = documentation
        self.labelnames = tuple(labelnames)
        # Pre-allocate one storage slot per allowed combination. The set is
        # closed: no new key can appear afterwards.
        self._keys: tuple[tuple[str, ...], ...] = tuple(
            tuple(values) for values in labelvalues
        )
        for key in self._keys:
            if len(key) != len(self.labelnames):
                raise ValueError(
                    f"label combination {key!r} does not match {self.labelnames!r}"
                )
        self._index: dict[tuple[str, ...], int] = {
            key: i for i, key in enumerate(self._keys)
        }
        if len(self._index) != len(self._keys):
            raise ValueError("duplicate label combinations are not allowed")
        self._lock = threading.Lock()

    def _slot(self, labels: Sequence[str]) -> int:
        key = tuple(labels)
        try:
            return self._index[key]
        except KeyError:
            # Refusing unknown values is what keeps cardinality bounded.
            raise ValueError(
                f"unregistered label combination {key!r} for metric "
                f"{self.name!r}; allowed combinations must be declared "
                f"up-front"
            ) from None

    def _render_header(self) -> list[str]:
        return [
            f"# HELP {self.name} {self.documentation}",
            f"# TYPE {self.name} {self.type_name}",
        ]

    def _render_labels(self, key: tuple[str, ...]) -> str:
        if not self.labelnames:
            return ""
        pairs = ",".join(
            f'{name}="{escape_label_value(value)}"'
            for name, value in zip(self.labelnames, key)
        )
        return "{" + pairs + "}"


class Counter(_BaseMetric):
    """Monotonic counter with one float per pre-declared label combination."""

    type_name = "counter"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str],
        labelvalues: Iterable[Sequence[str]],
    ) -> None:
        super().__init__(name, documentation, labelnames, labelvalues)
        self._values: list[float] = [0.0 for _ in self._keys]

    def inc(self, labels: Sequence[str], amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("counter can only increase")
        slot = self._slot(labels)
        with self._lock:
            self._values[slot] += amount

    def value(self, labels: Sequence[str]) -> float:
        slot = self._slot(labels)
        with self._lock:
            return self._values[slot]

    def render_lines(self) -> list[str]:
        with self._lock:
            snapshot = list(self._values)
        lines = self._render_header()
        for key, value in zip(self._keys, snapshot):
            if value == 0:
                # Storage stays pre-allocated and bounded; untouched
                # combinations are simply not emitted, keeping exposition
                # small regardless of how many combinations were declared.
                continue
            lines.append(
                f"{self.name}{self._render_labels(key)} {format_number(value)}"
            )
        return lines


class Gauge(_BaseMetric):
    """Gauge with one float per pre-declared label combination."""

    type_name = "gauge"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str],
        labelvalues: Iterable[Sequence[str]],
    ) -> None:
        super().__init__(name, documentation, labelnames, labelvalues)
        self._values: list[float] = [0.0 for _ in self._keys]

    def inc(self, labels: Sequence[str], amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("amount must be non-negative")
        slot = self._slot(labels)
        with self._lock:
            self._values[slot] += amount

    def dec(self, labels: Sequence[str], amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("amount must be non-negative")
        slot = self._slot(labels)
        with self._lock:
            self._values[slot] -= amount

    def set(self, labels: Sequence[str], value: float) -> None:
        slot = self._slot(labels)
        with self._lock:
            self._values[slot] = value

    def value(self, labels: Sequence[str]) -> float:
        slot = self._slot(labels)
        with self._lock:
            return self._values[slot]

    def render_lines(self) -> list[str]:
        with self._lock:
            snapshot = list(self._values)
        lines = self._render_header()
        for key, value in zip(self._keys, snapshot):
            if value == 0:
                continue
            lines.append(
                f"{self.name}{self._render_labels(key)} {format_number(value)}"
            )
        return lines


class Histogram(_BaseMetric):
    """Cumulative histogram with fixed buckets per label combination."""

    type_name = "histogram"

    def __init__(
        self,
        name: str,
        documentation: str,
        labelnames: Sequence[str],
        labelvalues: Iterable[Sequence[str]],
        buckets: Sequence[float] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, documentation, labelnames, labelvalues)
        boundaries = tuple(buckets)
        if list(boundaries) != sorted(boundaries) or len(set(boundaries)) != len(
            boundaries
        ):
            raise ValueError("buckets must be strictly increasing")
        self.boundaries: tuple[float, ...] = boundaries
        n = len(self._keys)
        # Cumulative bucket counts including the implicit +Inf bucket.
        self._buckets: list[list[int]] = [
            [0 for _ in range(len(boundaries) + 1)] for _ in range(n)
        ]
        self._sum: list[float] = [0.0 for _ in range(n)]
        self._count: list[int] = [0 for _ in range(n)]

    def observe(self, labels: Sequence[str], amount: float) -> None:
        if amount < 0 or math.isnan(amount):
            raise ValueError("histogram observation must be a finite >= 0 number")
        slot = self._slot(labels)
        with self._lock:
            counts = self._buckets[slot]
            # Every boundary >= amount is incremented; the trailing +Inf
            # bucket is always incremented.
            for i, boundary in enumerate(self.boundaries):
                if amount <= boundary:
                    counts[i] += 1
            counts[-1] += 1
            self._sum[slot] += amount
            self._count[slot] += 1

    def count(self, labels: Sequence[str]) -> int:
        slot = self._slot(labels)
        with self._lock:
            return self._count[slot]

    def sum(self, labels: Sequence[str]) -> float:
        slot = self._slot(labels)
        with self._lock:
            return self._sum[slot]

    def bucket_counts(self, labels: Sequence[str]) -> tuple[int, ...]:
        slot = self._slot(labels)
        with self._lock:
            return tuple(self._buckets[slot])

    def render_lines(self) -> list[str]:
        with self._lock:
            snapshot_buckets = [list(b) for b in self._buckets]
            snapshot_sum = list(self._sum)
            snapshot_count = list(self._count)
        lines = self._render_header()
        for key, counts, total_sum, total_count in zip(
            self._keys, snapshot_buckets, snapshot_sum, snapshot_count
        ):
            if total_count == 0:
                continue
            labels = self._render_labels(key)
            for boundary, cumulative in zip(self.boundaries, counts[:-1]):
                le = format_number(float(boundary))
                lines.append(
                    f'{self.name}_bucket{self._with_le(key, le)} {cumulative}'
                )
            lines.append(
                f'{self.name}_bucket{self._with_le(key, "+Inf")} {counts[-1]}'
            )
            lines.append(f"{self.name}_sum{labels} {format_number(total_sum)}")
            lines.append(f"{self.name}_count{labels} {total_count}")
        return lines

    def _with_le(self, key: tuple[str, ...], le: str) -> str:
        pairs = [
            f'{name}="{escape_label_value(value)}"'
            for name, value in zip(self.labelnames, key)
        ]
        pairs.append(f'le="{le}"')
        return "{" + ",".join(pairs) + "}"


class Registry:
    """Holds a fixed collection of metrics and renders Prometheus text."""

    content_type = CONTENT_TYPE_LATEST

    def __init__(self) -> None:
        self._metrics: dict[str, Metric] = {}
        self._lock = threading.Lock()

    def register(self, metric: Metric) -> None:
        with self._lock:
            if metric.name in self._metrics:
                # Duplicate registration would emit the same series twice.
                raise ValueError(f"metric {metric.name!r} already registered")
            self._metrics[metric.name] = metric

    def get(self, name: str) -> Metric | None:
        with self._lock:
            return self._metrics.get(name)

    def render(self) -> str:
        with self._lock:
            metrics = list(self._metrics.values())
        lines: list[str] = []
        for metric in metrics:
            lines.extend(metric.render_lines())
        return "\n".join(lines) + "\n" if lines else ""
