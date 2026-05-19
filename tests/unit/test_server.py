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
        # 2026-05-11: +2 tools (dispatch_subagent_cheap + dispatch_subagent_inherit).
        assert "dispatch_subagent_cheap" in names
        assert "dispatch_subagent_inherit" in names
        assert len(names) == 17


# ─── 2026-05-12 multi-token rotation tests ─────────────────────────────────


@pytest.mark.asyncio
async def test_previous_token_accepted_during_rotation(deps: MagicMock) -> None:
    """When bearer_token_previous is set (rotation window), BOTH the
    primary and the previous token authenticate. This is the property
    that turns a token rotation from a 16-hour incident into a no-op:
    clients still using the old token keep working while their config
    files are being rewritten one by one.
    """
    app = build_app(
        deps=deps,
        bearer_token="new-token",
        bearer_token_previous="old-token",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # Primary works.
        r = await c.get("/mcp/tools/list", headers={"authorization": "Bearer new-token"})
        assert r.status_code == 200, "primary token must authenticate"
        # Previous works during rotation.
        r = await c.get("/mcp/tools/list", headers={"authorization": "Bearer old-token"})
        assert r.status_code == 200, "previous token must authenticate during rotation"
        # Genuine garbage still gets rejected.
        r = await c.get("/mcp/tools/list", headers={"authorization": "Bearer not-a-token"})
        assert r.status_code == 401, "unknown token must be rejected"


@pytest.mark.asyncio
async def test_previous_token_rejected_once_rotation_window_closes(
    deps: MagicMock,
) -> None:
    """After fleet-rotate-token retires the previous token (clears
    FLEET_BEARER_TOKEN_PREVIOUS in .env and restarts), only the primary
    is accepted.
    """
    app = build_app(deps=deps, bearer_token="new-token", bearer_token_previous="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/mcp/tools/list", headers={"authorization": "Bearer new-token"})
        assert r.status_code == 200
        r = await c.get("/mcp/tools/list", headers={"authorization": "Bearer old-token"})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_empty_bearer_token_disables_auth_entirely(deps: MagicMock) -> None:
    """Backward-compat: configuration without any token (e.g. local dev)
    leaves _auth a no-op. Same posture as the pre-rotation code path.
    """
    app = build_app(deps=deps, bearer_token="", bearer_token_previous="")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # No auth header at all — should still 200.
        r = await c.get("/mcp/tools/list")
        assert r.status_code == 200


# ─── 2026-05-19 A2: /metrics endpoint ───────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_200(deps: MagicMock) -> None:
    """/metrics is reachable without auth and returns Prometheus text."""
    app = build_app(deps=deps, bearer_token="tok")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/metrics")
        assert r.status_code == 200
        # Either real prometheus text or the disabled-shim comment.
        assert r.headers["content-type"].startswith("text/plain")
        # When prometheus_client is installed (CI default), at least one
        # of our metrics should appear in the output as a help/type line.
        try:
            import prometheus_client  # noqa: F401

            assert "fleet_dispatches_total" in r.text or "fleet_worktrees_active" in r.text
        except ImportError:
            assert "disabled" in r.text
