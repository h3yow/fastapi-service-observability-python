"""Minimal, concurrency-safe Prometheus text-format metrics registry."""
from __future__ import annotations

from threading import Lock
from typing import Iterable

LabelTuple = tuple[tuple[str, str], ...]


class MetricsRegistry:
    def __init__(self) -> None:
        self._lock: Lock = Lock()
        self._enabled: dict[str, bool] = {}
        self._counters: dict[str, dict] = {}
        self._gauges: dict[str, dict] = {}
        self._histograms: dict[str, dict] = {}

    def register_counter(self, name: str, help_text: str, labelnames: tuple[str, ...], *, enabled: bool = True) -> None:
        raise NotImplementedError

    def register_gauge(self, name: str, help_text: str, labelnames: tuple[str, ...], *, enabled: bool = True) -> None:
        raise NotImplementedError

    def register_histogram(
        self,
        name: str,
        help_text: str,
        labelnames: tuple[str, ...],
        buckets: tuple[float, ...],
        *,
        enabled: bool = True,
    ) -> None:
        raise NotImplementedError

    def set_enabled(self, name: str, enabled: bool) -> None:
        raise NotImplementedError

    def is_enabled(self, name: str) -> bool:
        raise NotImplementedError

    def inc_counter(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        raise NotImplementedError

    def inc_gauge(self, name: str, labels: LabelTuple, value: float = 1.0) -> None:
        raise NotImplementedError

    def observe(self, name: str, labels: LabelTuple, value: float) -> None:
        raise NotImplementedError

    def render(self) -> str:
        raise NotImplementedError

    def _render_help(self, name: str, help_text: str, mtype: str) -> Iterable[str]:
        raise NotImplementedError
