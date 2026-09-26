"""Application-level smoke tests (construction + exposition wire-up)."""
from __future__ import annotations

import asyncio

from conftest import asgi_client, make_app


def test_app_serves_root_and_metrics() -> None:
    async def scenario() -> None:
        app = make_app()
        async with asgi_client(app) as c:
            root = await c.get("/")
            metrics = await c.get("/metrics")
        print("input: GET / then GET /metrics")
        print(f"judgement: root status={root.status_code}; "
              f"metrics status={metrics.status_code}, "
              f"content-type={metrics.headers['content-type']}")
        assert root.status_code == 200 and root.json() == {"hello": "world"}
        assert metrics.status_code == 200
        assert "# TYPE http_requests_total counter" in metrics.text

    asyncio.run(scenario())
