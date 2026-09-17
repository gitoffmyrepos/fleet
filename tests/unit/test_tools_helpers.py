"""Unit tests for the tools.py helpers and the background supervisor.

These guard the bits that are easy to break silently: the required-arg
check, the cwd fallback that still exists for the Hermes harness, and the
supervisor whose whole job is to make sure a fire-and-forget dispatch
cannot fail invisibly.
"""

from __future__ import annotations

import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet import tools as tools_mod
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


# ─── _require / _new_task_id ───────────────────────────────────────────────


@pytest.mark.parametrize("args", [{}, {"task": None}, {"task": ""}])
def test_require_rejects_missing_empty_and_none(args: dict) -> None:
    with pytest.raises(ToolError, match="'task' argument is required"):
        tools_mod._require(args, "task")


def test_require_returns_the_value_when_present() -> None:
    assert tools_mod._require({"task": "go"}, "task") == "go"


def test_new_task_id_is_prefixed_and_unique() -> None:
    a, b = tools_mod._new_task_id(), tools_mod._new_task_id()
    assert a.startswith("task_") and b.startswith("task_")
    assert a != b


# ─── _resolve_cwd ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", ["cwd", "workdir", "repo_path"])
def test_resolve_cwd_accepts_each_accepted_key(key: str) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # no warning expected on the happy path
        assert tools_mod._resolve_cwd({key: "/tmp/x"}) == "/tmp/x"


def test_resolve_cwd_prefers_cwd_over_the_aliases() -> None:
    assert tools_mod._resolve_cwd({"cwd": "/a", "workdir": "/b", "repo_path": "/c"}) == "/a"


def test_resolve_cwd_falls_back_to_fx_with_a_deprecation_warning() -> None:
    with pytest.warns(DeprecationWarning, match="defaulting to FX repo"):
        assert tools_mod._resolve_cwd({}) == tools_mod._FX_LITERAL


def test_resolve_cwd_warns_when_the_old_literal_is_passed_explicitly() -> None:
    with pytest.warns(DeprecationWarning, match="literal old FX default"):
        assert tools_mod._resolve_cwd({"cwd": tools_mod._FX_LITERAL}) == tools_mod._FX_LITERAL


# ─── _supervise_background_dispatch ────────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_is_silent_when_the_dispatch_succeeds() -> None:
    telemetry = AsyncMock()

    async def ok() -> str:
        return "fine"

    await tools_mod._supervise_background_dispatch(
        ok(), task_id="t1", telemetry=telemetry, label="subagent"
    )
    telemetry.failure.assert_not_called()


@pytest.mark.asyncio
async def test_supervisor_reports_a_failed_dispatch_as_telemetry() -> None:
    telemetry = AsyncMock()

    async def boom() -> None:
        raise RuntimeError("exploded")

    await tools_mod._supervise_background_dispatch(
        boom(), task_id="t2", telemetry=telemetry, label="swarm"
    )
    telemetry.failure.assert_awaited_once()
    kwargs = telemetry.failure.call_args[1]
    assert kwargs["task_id"] == "t2"
    assert "RuntimeError: exploded" in kwargs["reason"]
    assert kwargs["body"] == {"label": "swarm"}


@pytest.mark.asyncio
async def test_supervisor_swallows_a_failing_telemetry_backend() -> None:
    """A broken telemetry sink must not turn into a second unhandled error."""
    telemetry = AsyncMock()
    telemetry.failure.side_effect = RuntimeError("sink down")

    async def boom() -> None:
        raise ValueError("original")

    await tools_mod._supervise_background_dispatch(
        boom(), task_id="t3", telemetry=telemetry, label="phase"
    )
    telemetry.failure.assert_awaited_once()


# ─── registry dispatch surface ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_tool_name_raises_tool_error(deps: MagicMock) -> None:
    with pytest.raises(ToolError, match="unknown"):
        await ToolRegistry(deps).call("does_not_exist", {})


@pytest.mark.asyncio
async def test_circuit_close_reports_the_breaker_state(deps: MagicMock) -> None:
    deps.circuits.close.return_value = True
    out = await ToolRegistry(deps).call("circuit_close", {"name": "ruflo"})
    assert out == {"name": "ruflo", "closed": True}
