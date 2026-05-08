from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.server import build_app


@pytest.mark.asyncio
async def test_dashboard_renders_html() -> None:
    deps = MagicMock()
    deps.graphiti.search_facts = AsyncMock(
        return_value=[
            {
                "id": "ep1",
                "kind": "fleet_dispatch_completed",
                "body": {"ok": True, "duration_seconds": 12.0},
            },
        ]
    )
    deps.circuits.snapshot_all = MagicMock(
        return_value=[
            {
                "name": "ruflo",
                "state": "closed",
                "failure_count_in_window": 0,
                "opened_at": None,
            },
        ]
    )
    deps.registry.size = MagicMock(return_value=212)
    deps.registry.is_stale = MagicMock(return_value=False)
    app = build_app(deps=deps, bearer_token="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard")
        assert r.status_code == 200
        body = r.text
        assert "Fleet" in body
        assert "ruflo" in body
        assert "212" in body


@pytest.mark.asyncio
async def test_dashboard_metrics_endpoint_returns_json() -> None:
    deps = MagicMock()
    deps.graphiti.search_facts = AsyncMock(return_value=[])
    deps.circuits.snapshot_all = MagicMock(return_value=[])
    deps.registry.size = MagicMock(return_value=0)
    deps.registry.is_stale = MagicMock(return_value=True)
    app = build_app(deps=deps, bearer_token="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/dashboard/metrics.json")
        assert r.status_code == 200
        m = r.json()
        assert m["registry"]["size"] == 0
        assert m["registry"]["stale"] is True
