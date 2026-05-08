from unittest.mock import AsyncMock, MagicMock

import pytest

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


def test_lists_14_tools(deps: MagicMock) -> None:
    r = ToolRegistry(deps)
    names = r.list_tool_names()
    assert len(names) == 14
    expected = {
        "route",
        "dispatch_swarm",
        "dispatch_phase",
        "dispatch_subagent",
        "dispatch_verify",
        "ship",
        "status",
        "explain",
        "cache_lookup",
        "list_agents",
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
