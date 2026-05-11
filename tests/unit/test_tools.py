from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet.dispatcher.base import DispatchResult
from fleet.tools import ToolError, ToolRegistry


@pytest.fixture
def deps() -> MagicMock:
    d = MagicMock()
    d.router = AsyncMock()
    d.cache = AsyncMock()
    d.registry = MagicMock()
    d.swarm = AsyncMock()
    d.phase = AsyncMock()
    d.subagent = AsyncMock()
    d.verify = AsyncMock()
    d.telemetry = AsyncMock()
    d.graphiti = AsyncMock()
    d.circuits = MagicMock()
    return d


def test_lists_17_tools(deps: MagicMock) -> None:
    """2026-05-11: two new dispatchers landed for symbiosis with Hermes —
    dispatch_subagent_cheap (model routing) and dispatch_subagent_inherit
    (MCP/tool allowlist inheritance). See /tmp/hermes-vs-fleet.md §4.
    """
    r = ToolRegistry(deps)
    names = r.list_tool_names()
    assert len(names) == 17
    expected = {
        "route",
        "dispatch_swarm",
        "dispatch_phase",
        "dispatch_subagent",
        "dispatch_subagent_cheap",
        "dispatch_subagent_inherit",
        "dispatch_verify",
        "ship",
        "status",
        "explain",
        "cache_lookup",
        "list_agents",
        "list_skills",
        "register_agent",
        "telemetry",
        "cancel",
        "circuit_close",
    }
    assert set(names) == expected


def test_unknown_tool_raises(deps: MagicMock) -> None:
    import asyncio

    r = ToolRegistry(deps)
    with pytest.raises(ToolError, match="unknown"):
        asyncio.run(r.call("nonsense", {}))


@pytest.mark.asyncio
async def test_route_tool_invokes_router(deps: MagicMock) -> None:
    deps.router.route = AsyncMock(
        return_value=MagicMock(
            kind="swarm",
            confidence=0.9,
            reason="r",
            via="heuristic",
            suggested_agents=20,
            suggested_topology="parallel",
            degraded=False,
        )
    )
    r = ToolRegistry(deps)
    out = await r.call("route", {"task": "audit", "task_id": "t1"})
    assert out["kind"] == "swarm"
    assert out["confidence"] == 0.9


@pytest.mark.asyncio
async def test_circuit_close_calls_registry(deps: MagicMock) -> None:
    deps.circuits.close = MagicMock(return_value=True)
    r = ToolRegistry(deps)
    out = await r.call("circuit_close", {"name": "ruflo"})
    assert out["closed"] is True
    deps.circuits.close.assert_called_once_with("ruflo")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,args,expected_keys",
    [
        # 2026-05-11 (opt-1): every dispatcher now requires explicit cwd.
        (
            "dispatch_swarm",
            {"task": "x", "cwd": "/tmp/wd"},
            {"task_id", "cache_hit", "ok", "summary", "error"},
        ),
        (
            "dispatch_phase",
            {"task": "x", "cwd": "/tmp/wd"},
            {"task_id", "ok", "summary", "error"},
        ),
        (
            "dispatch_subagent",
            {"task": "x", "cwd": "/tmp/wd"},
            {"task_id", "ok", "summary", "error"},
        ),
        (
            "dispatch_verify",
            {"task": "x", "cwd": "/tmp/wd"},
            {"task_id", "ok", "summary", "error"},
        ),
        ("ship", {"cwd": "/tmp/wd"}, {"task_id", "ok", "summary", "error"}),
        ("status", {}, {"items", "circuits"}),
        ("explain", {"task_id": "t1"}, {"task_id", "chain"}),
        ("cache_lookup", {"task": "x"}, {"hash", "hit", "entry"}),
        ("list_agents", {}, {"stale", "agents"}),
        ("register_agent", {}, {"accepted", "reason"}),
        ("telemetry", {"task_id": "t1"}, {"ok"}),
        ("cancel", {"task_id": "t1"}, {"task_id", "cancel_requested"}),
        ("circuit_close", {"name": "ruflo"}, {"name", "closed"}),
    ],
)
async def test_handler_response_keys(
    tool: str, args: dict, expected_keys: set, deps: MagicMock
) -> None:
    """Smoke test: each handler returns the prescribed keys (regression guard)."""
    fake_result = DispatchResult(ok=True, task_id="t1", summary={}, error="")
    deps.swarm.dispatch = AsyncMock(return_value=fake_result)
    deps.phase.dispatch = AsyncMock(return_value=fake_result)
    deps.subagent.dispatch = AsyncMock(return_value=fake_result)
    deps.verify.dispatch = AsyncMock(return_value=fake_result)
    deps.cache.lookup = AsyncMock(return_value=None)
    deps.cache.write = AsyncMock(return_value="ep")
    deps.graphiti.search_facts = AsyncMock(return_value=[])
    deps.registry.all = MagicMock(return_value=[])
    deps.registry.is_stale = MagicMock(return_value=False)
    deps.circuits.snapshot_all = MagicMock(return_value=[])
    deps.circuits.close = MagicMock(return_value=True)

    r = ToolRegistry(deps)
    out = await r.call(tool, args)
    assert (
        set(out.keys()) >= expected_keys
    ), f"{tool} missing keys: {expected_keys - set(out.keys())}"
