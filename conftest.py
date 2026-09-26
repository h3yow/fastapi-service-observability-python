"""Shared pytest configuration and helpers.

Every test logs its inputs and its verdict basis (which values were compared
against which expectations), as required for the observability suite.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest

logger = logging.getLogger("observability-tests")
logger.setLevel(logging.INFO)

_SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})? (?P<value>\S+)$")
_LABEL_RE = re.compile(r'(\w+)="((?:[^"\\]|\\.)*)"')


@pytest.fixture(autouse=True)
def _log_test_case(request: pytest.FixtureRequest) -> Any:
    logger.info("CASE START %s", request.node.nodeid)
    yield
    logger.info("CASE END   %s -> PASSED (assertions above hold)", request.node.nodeid)


def pytest_configure(config: pytest.Config) -> None:
    """Keep third-party request spam out of the evidence log."""
    for noisy in ("httpx", "httpcore", "urllib3", "starlette", "fastapi"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def parse_samples(text: str) -> dict[str, float]:
    """Parse Prometheus text into ``{metric{labels}: value}`` sample lines.

    Only numeric sample lines are returned (``# HELP`` / ``# TYPE`` headers
    are ignored), which is exactly what the conservation tests compare.
    """
    samples: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        assert match is not None, f"unparseable metric line: {line!r}"
        value = match.group("value")
        key = line.rsplit(" ", 1)[0]
        if value in ("NaN", "+Inf", "-Inf"):
            continue
        samples[key] = float(value)
    return samples


def label_dict(label_section: str) -> dict[str, str]:
    """Decode the ``k="v"`` section inside a metric sample line."""
    return {m.group(1): m.group(2) for m in _LABEL_RE.finditer(label_section)}


def counter_total(samples: dict[str, float], metric: str = "http_requests_total") -> float:
    return sum(
        v for key, v in samples.items()
        if key.startswith(metric + "{") or key == metric
    )


pytest_plugins = ("anyio",)

