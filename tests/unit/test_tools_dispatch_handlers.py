"""Unit tests for the dispatch_subagent handler family.

Covers the argument plumbing (worktree isolation by default, model alias
mapping for the cheap variant) and the background mode that returns a
task id immediately instead of awaiting a multi-minute dispatch. The
dispatchers themselves are AsyncMocks — nothing spawns a process.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet.dispatcher.base import DispatchResult
from fleet.tools import ToolRegistry


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
    d.subagent.dispatch.return_value = DispatchResult(ok=True, task_id="t", stdout="done")
    return d


@pytest.mark.asyncio
async def test_dispatch_subagent_defaults_to_worktree_isolation(deps: MagicMock) -> None:
    out = await ToolRegistry(deps).call(
        "dispatch_subagent", {"task": "fix the parser", "cwd": "/tmp"}
    )
    assert out["ok"] is True
    kwargs = deps.subagent.dispatch.call_args[1]
    assert kwargs["isolation"] == "worktree"
    assert kwargs["cwd"] == "/tmp"
    assert kwargs["task"] == "fix the parser"


@pytest.mark.asyncio
async def test_dispatch_subagent_honours_an_explicit_isolation_override(
    deps: MagicMock,
) -> None:
    await ToolRegistry(deps).call(
        "dispatch_subagent", {"task": "t", "cwd": "/tmp", "isolation": None}
    )
    assert deps.subagent.dispatch.call_args[1]["isolation"] is None


@pytest.mark.asyncio
async def test_dispatch_subagent_uses_the_caller_supplied_task_id(deps: MagicMock) -> None:
    out = await ToolRegistry(deps).call(
        "dispatch_subagent", {"task": "t", "cwd": "/tmp", "task_id": "mine-1"}
    )
    assert out["task_id"] == "mine-1"


@pytest.mark.asyncio
async def test_background_mode_returns_started_without_awaiting(deps: MagicMock) -> None:
    started = asyncio.Event()

    async def slow(**_: object) -> DispatchResult:
        started.set()
        await asyncio.sleep(0.05)
        return DispatchResult(ok=True, task_id="t", stdout="late")

    deps.subagent.dispatch = slow
    out = await ToolRegistry(deps).call(
        "dispatch_subagent", {"task": "t", "cwd": "/tmp", "run_in_background": True}
    )
    assert out["status"] == "started"
    assert out["task_id"]
    await asyncio.sleep(0.1)  # let the supervised task finish cleanly
    assert started.is_set()


@pytest.mark.asyncio
async def test_cheap_variant_maps_friendly_aliases_to_model_ids(deps: MagicMock) -> None:
    await ToolRegistry(deps).call(
        "dispatch_subagent_cheap", {"task": "t", "cwd": "/tmp", "model": "haiku"}
    )
    assert deps.subagent.dispatch.call_args[1]["model"] == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_cheap_variant_passes_through_explicit_model_snapshots(
    deps: MagicMock,
) -> None:
    await ToolRegistry(deps).call(
        "dispatch_subagent_cheap",
        {"task": "t", "cwd": "/tmp", "model": "claude-haiku-4-5-20251001"},
    )
    assert deps.subagent.dispatch.call_args[1]["model"] == "claude-haiku-4-5-20251001"


@pytest.mark.asyncio
async def test_dispatch_rejects_a_missing_task(deps: MagicMock) -> None:
    from fleet.tools import ToolError

    with pytest.raises(ToolError, match="'task' argument is required"):
        await ToolRegistry(deps).call("dispatch_subagent", {"cwd": "/tmp"})
