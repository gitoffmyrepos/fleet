"""Unit tests for the dispatch_local MCP tool (local-fleet bridge).

The local-fleet CLI is mocked at the asyncio.create_subprocess_exec level —
no test may spawn a real process or touch /home/kelvin/.local/bin/local-fleet.
Style follows tests/unit/test_tools.py (deps fixture + async tool calls).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet import tools as tools_mod
from fleet.tools import ToolRegistry


@pytest.fixture
def deps() -> MagicMock:
    """Same shape as tests/unit/test_tools.py — dispatch_local does not
    touch the deps namespace, but ToolRegistry is constructed with it."""
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


def _make_proc(
    *, rc: int = 0, stdout: bytes = b"[]", stderr: bytes = b"", communicate=None
) -> MagicMock:
    """Fake subprocess: communicate/kill/wait stand in for asyncio.Process."""
    proc = MagicMock()
    proc.returncode = rc
    proc.communicate = (
        communicate if communicate is not None else AsyncMock(return_value=(stdout, stderr))
    )
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=rc)
    return proc


@pytest.mark.asyncio
async def test_success_parses_json(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    """Happy path: CLI JSON array on stdout is parsed and returned verbatim,
    defaults are forwarded (base=master, timeout=2400), hard wait timeout is
    per-task timeout + 120s."""
    results = [
        {
            "subtask": "add error handling to x",
            "ok": True,
            "worktree": "/tmp/local-fleet-worktrees/abc123",
            "branch": "local-fleet/abc123",
            "pr_url": None,
            "commits": ["abc1234 feat: add error handling"],
            "uncommitted_changes": False,
            "output_tail": "done",
            "duration_seconds": 1.0,
        }
    ]
    proc = _make_proc(
        rc=0,
        stdout=json.dumps(results).encode("utf-8"),
        stderr=b"recap: 1 ok, 0 failed\n",
    )
    calls: list[tuple] = []

    async def fake_exec(*argv, **kwargs):
        calls.append((argv, kwargs))
        return proc

    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", fake_exec)

    # Capture the hard wait timeout passed to asyncio.wait_for.
    real_wait_for = asyncio.wait_for
    captured: dict = {}

    async def fake_wait_for(coro, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return await real_wait_for(coro, *args, **kwargs)

    monkeypatch.setattr("fleet.tools.asyncio.wait_for", fake_wait_for)

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": ["add error handling to x"]},
    )

    assert out["ok"] is True
    assert out["results"] == results
    assert out["stderr_tail"] == "recap: 1 ok, 0 failed\n"
    assert out["task_id"].startswith("task_")
    # Hard timeout = per-task timeout (default 2400) + 120s grace.
    assert captured["timeout"] == 2520.0
    # Spawned exactly once, with the documented argv shape.
    assert len(calls) == 1
    argv = calls[0][0]
    assert argv[0] == tools_mod._LOCAL_FLEET_BIN
    assert argv[1] == "dispatch"
    assert argv[argv.index("--repo") + 1] == str(tmp_path)
    assert argv[argv.index("--base") + 1] == "master"
    assert argv[argv.index("--timeout") + 1] == "2400.0"
    assert argv.count("--task") == 1
    assert argv[argv.index("--task") + 1] == "add error handling to x"


@pytest.mark.asyncio
async def test_more_than_six_tasks_rejected_without_spawning(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    fake_exec = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", fake_exec)

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": [f"task {i}" for i in range(7)]},
    )

    assert out["ok"] is False
    assert "too many" in out["error"]
    assert out["results"] == []
    assert out["task_id"].startswith("task_")
    fake_exec.assert_not_called()


@pytest.mark.asyncio
async def test_missing_repo_dir_rejected(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    fake_exec = AsyncMock(side_effect=AssertionError("must not spawn"))
    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", fake_exec)

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path / "nope"), "tasks": ["fix x"]},
    )

    assert out["ok"] is False
    assert "not an existing directory" in out["error"]
    assert out["results"] == []
    fake_exec.assert_not_called()


@pytest.mark.asyncio
async def test_nonzero_exit_returns_ok_false_with_stderr_tail(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    proc = _make_proc(rc=1, stdout=b"", stderr=b"x" * 990 + b"BOOM")
    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": ["fix a", "add b"], "base": "main", "timeout": 60},
    )

    assert out["ok"] is False
    assert out["results"] == []
    assert out["error"] == "local-fleet exited 1"
    assert out["stderr_tail"].endswith("BOOM")
    assert len(out["stderr_tail"]) == 800


@pytest.mark.asyncio
async def test_bad_json_stdout_returns_ok_false(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    proc = _make_proc(rc=0, stdout=b"not json at all", stderr=b"recap")
    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": ["fix x"]},
    )

    assert out["ok"] is False
    assert "JSON" in out["error"]
    assert out["results"] == []
    assert out["stdout_tail"] == "not json at all"


@pytest.mark.asyncio
async def test_custom_base_and_timeout_forwarded(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    proc = _make_proc(rc=0, stdout=b"[]", stderr=b"")
    calls: list[tuple] = []

    async def fake_exec(*argv, **kwargs):
        calls.append((argv, kwargs))
        return proc

    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", fake_exec)

    real_wait_for = asyncio.wait_for
    captured: dict = {}

    async def fake_wait_for(coro, *args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return await real_wait_for(coro, *args, **kwargs)

    monkeypatch.setattr("fleet.tools.asyncio.wait_for", fake_wait_for)

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": ["a"], "base": "main", "timeout": 60},
    )

    assert out["ok"] is True
    assert out["results"] == []
    argv = calls[0][0]
    assert argv[argv.index("--base") + 1] == "main"
    assert argv[argv.index("--timeout") + 1] == "60.0"
    # Hard wait timeout = 60 + 120.
    assert captured["timeout"] == 180.0


@pytest.mark.asyncio
async def test_hard_timeout_kills_process(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    proc = _make_proc(rc=0, stdout=b"[]", stderr=b"")
    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr(
        "fleet.tools.asyncio.wait_for", AsyncMock(side_effect=TimeoutError("hard timeout"))
    )

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": ["slow task"]},
    )

    assert out["ok"] is False
    assert "hard timeout" in out["error"]
    assert out["results"] == []
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_spawn_failure_returns_ok_false(
    tmp_path, monkeypatch: pytest.MonkeyPatch, deps: MagicMock
) -> None:
    fake_exec = AsyncMock(side_effect=FileNotFoundError("local-fleet not found"))
    monkeypatch.setattr("fleet.tools.asyncio.create_subprocess_exec", fake_exec)

    r = ToolRegistry(deps)
    out = await r.call(
        "dispatch_local",
        {"repo": str(tmp_path), "tasks": ["fix x"]},
    )

    assert out["ok"] is False
    assert "could not launch local-fleet" in out["error"]
    assert out["results"] == []
