"""Application factory with demo routes used to exercise the metrics."""
from __future__ import annotations

import asyncio

from fastapi import FastAPI

from .setup import setup_observability


def create_app(*, request_timeout: float | None = 5.0, duration_enabled: bool = True) -> FastAPI:
    raise NotImplementedError
