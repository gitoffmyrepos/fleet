from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from fleet.server import build_app


@pytest.fixture
def deps() -> MagicMock:
    d = MagicMock()
    d.router.route = AsyncMock(
        return_value=MagicMock(
            kind="subagent",
            confidence=0.4,
            reason="r",
            via="heuristic",
            suggested_agents=None,
            suggested_topology=None,
            degraded=False,
        )
    )
    d.circuits.snapshot_all = MagicMock(return_value=[])
    return d


@pytest.mark.asyncio
async def test_health_returns_200() -> None:
    app = build_app(deps=MagicMock(), bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health")
        assert r.status_code == 200
        assert r.json()["ok"] is True


@pytest.mark.asyncio
async def test_missing_bearer_returns_401(deps: MagicMock) -> None:
    app = build_app(deps=deps, bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/mcp/tools/call", json={"name": "route", "arguments": {"task": "x"}})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_wrong_bearer_returns_401(deps: MagicMock) -> None:
    app = build_app(deps=deps, bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/mcp/tools/call",
            json={"name": "route", "arguments": {"task": "x"}},
            headers={"authorization": "Bearer nope"},
        )
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_correct_bearer_calls_tool(deps: MagicMock) -> None:
    app = build_app(deps=deps, bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/mcp/tools/call",
            json={"name": "route", "arguments": {"task": "x"}},
            headers={"authorization": "Bearer tok"},
        )
        assert r.status_code == 200
        assert r.json()["result"]["kind"] == "subagent"


@pytest.mark.asyncio
async def test_blank_token_means_open(deps: MagicMock) -> None:
    app = build_app(deps=deps, bearer_token="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/mcp/tools/call", json={"name": "route", "arguments": {"task": "x"}})
        assert r.status_code == 200


@pytest.mark.asyncio
async def test_unknown_tool_returns_400(deps: MagicMock) -> None:
    app = build_app(deps=deps, bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/mcp/tools/call",
            json={"name": "nonsense", "arguments": {}},
            headers={"authorization": "Bearer tok"},
        )
        assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_tools_endpoint(deps: MagicMock) -> None:
    app = build_app(deps=deps, bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/mcp/tools/list", headers={"authorization": "Bearer tok"})
        assert r.status_code == 200
        names = r.json()["tools"]
        assert "route" in names
        assert "list_skills" in names
        assert len(names) == 15
