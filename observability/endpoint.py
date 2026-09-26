"""Prometheus exposition endpoint."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .registry import MetricsRegistry


def build_metrics_router(registry: MetricsRegistry) -> APIRouter:
    raise NotImplementedError


async def render_metrics(registry: MetricsRegistry) -> Any:
    raise NotImplementedError
