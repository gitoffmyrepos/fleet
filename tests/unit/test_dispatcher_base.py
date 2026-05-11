from typing import Any
from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher.base import DispatcherBase, DispatchResult


@pytest.fixture
def reg() -> CircuitRegistry:
    return CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)


@pytest.fixture
def tel() -> AsyncMock:
    t = AsyncMock()
    t.start = AsyncMock(return_value="ep_s")
    t.end = AsyncMock(return_value="ep_e")
    t.failure = AsyncMock(return_value="ep_f")
    t.event = AsyncMock(return_value="ep_v")
    return t


class _Echo(DispatcherBase):
    upstream_name = "echo"

    def cli_args(self, **kw: Any) -> list[str]:
        return ["/bin/sh", "-c", f"echo '{kw['msg']}'"]

    def parse_summary(self, stdout: str, stderr: str = "", **kw: Any) -> dict[str, Any]:
        return {"out": stdout.strip()}


@pytest.mark.asyncio
async def test_happy_path_echo(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = _Echo(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(task_id="t1", msg="hello")
    assert isinstance(result, DispatchResult)
    assert result.ok is True
    assert "hello" in result.summary["out"]
    tel.start.assert_awaited()
    tel.end.assert_awaited()


@pytest.mark.asyncio
async def test_nonzero_exit_marks_failure(reg: CircuitRegistry, tel: AsyncMock) -> None:
    class _Fail(DispatcherBase):
        upstream_name = "fail"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "exit 7"]

        def parse_summary(self, stdout: str, stderr: str = "", **kw: Any) -> dict[str, Any]:
            return {}

    d = _Fail(circuits=reg, telemetry=tel, timeout_seconds=5)
    result = await d.dispatch(task_id="t2")
    assert result.ok is False
    assert "exit code 7" in result.error or "exit 7" in result.error
    tel.failure.assert_awaited()


@pytest.mark.asyncio
async def test_timeout_returns_partial(reg: CircuitRegistry, tel: AsyncMock) -> None:
    class _Sleep(DispatcherBase):
        upstream_name = "sleep"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "sleep 3"]

        def parse_summary(self, stdout: str, stderr: str = "", **kw: Any) -> dict[str, Any]:
            return {}

    d = _Sleep(circuits=reg, telemetry=tel, timeout_seconds=1)
    result = await d.dispatch(task_id="t3")
    assert result.ok is False
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_open_circuit_blocks_dispatch(reg: CircuitRegistry, tel: AsyncMock) -> None:
    cb = reg.get("echo")
    for _ in range(3):
        cb.record_failure()
    d = _Echo(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(task_id="t4", msg="hi")
    assert result.ok is False
    assert "circuit" in result.error.lower()


@pytest.mark.asyncio
async def test_3_failures_trip_breaker(reg: CircuitRegistry, tel: AsyncMock) -> None:
    class _Fail(DispatcherBase):
        upstream_name = "trip"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "exit 1"]

        def parse_summary(self, stdout: str, stderr: str = "", **kw: Any) -> dict[str, Any]:
            return {}

    d = _Fail(circuits=reg, telemetry=tel, timeout_seconds=5)
    for i in range(3):
        await d.dispatch(task_id=f"t{i}")
    assert reg.get("trip").snapshot()["state"] == "open"


def test_abstract_cannot_instantiate_without_cli_args() -> None:
    """`DispatcherBase` is abstract; subclasses without cli_args raise on instantiation."""

    class _Bad(DispatcherBase):
        upstream_name = "bad"
        # Intentionally does NOT override cli_args

    reg = CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)
    tel = AsyncMock()
    with pytest.raises(TypeError, match="abstract"):
        _Bad(circuits=reg, telemetry=tel, timeout_seconds=5)


@pytest.mark.asyncio
async def test_default_parse_summary_returns_stdout_tail(
    reg: CircuitRegistry, tel: AsyncMock
) -> None:
    """Subclass that doesn't override parse_summary gets the default tail-500 wrapper."""

    class _NoSummary(DispatcherBase):
        upstream_name = "no_summary"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "echo hello world"]

    d = _NoSummary(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(task_id="t_default_summary")
    assert result.ok is True
    assert "stdout_tail" in result.summary
    assert "hello world" in result.summary["stdout_tail"]
    assert result.duration_seconds > 0  # also verifies Fix D


@pytest.mark.asyncio
async def test_timeout_sigkill_escalation_when_sigterm_ignored(
    reg: CircuitRegistry, tel: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cover the SIGKILL escalation path: process ignores SIGTERM, gets SIGKILLed after 5s grace."""
    import asyncio as _asyncio

    class _Stubborn(DispatcherBase):
        upstream_name = "stubborn"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "sleep 30"]

    # Force the inner wait_for(proc.wait()) to also time out, exercising kill() branch.
    real_wait_for = _asyncio.wait_for
    call_count = {"n": 0}

    async def fake_wait_for(coro: Any, timeout: float) -> Any:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # First call: outer communicate() — let it time out fast.
            return await real_wait_for(coro, timeout=0.1)
        if call_count["n"] == 2:
            # Second call: inner proc.wait() after terminate — also force timeout.
            raise TimeoutError
        return await real_wait_for(coro, timeout=timeout)

    monkeypatch.setattr(_asyncio, "wait_for", fake_wait_for)

    d = _Stubborn(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(task_id="t_sigkill")
    assert result.ok is False
    assert result.error == "timeout"
    assert result.duration_seconds > 0


# ---------------------------------------------------------------------------
# Worktree isolation (2026-05-10) — parallel subagents on same cwd race fix
# ---------------------------------------------------------------------------


async def _init_git_repo(tmp_path: Any) -> str:
    """Create a fresh git repo with one commit + a fake remote. Returns path."""
    import subprocess

    repo = tmp_path / "src"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)

    # bare remote so push works (file:// remote URLs are real to git)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "master"], cwd=repo, check=True)
    return str(repo)


@pytest.mark.asyncio
async def test_worktree_isolation_creates_separate_tree(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Any
) -> None:
    """isolation='worktree' creates a git worktree in /tmp/fleet-worktrees/<task_id>
    and the subprocess runs there — the source repo's working tree is untouched.
    """
    repo = await _init_git_repo(tmp_path)

    class _Writer(DispatcherBase):
        upstream_name = "writer"

        def cli_args(self, **kw: Any) -> list[str]:
            # The subprocess writes a marker file in its CWD so we can verify
            # which working tree it actually ran in.
            return ["/bin/sh", "-c", "echo isolated > marker.txt"]

    d = _Writer(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_iso_create",
        cwd=repo,
        task="test isolation",
        isolation="worktree",
        auto_commit=True,
    )
    assert result.ok is True
    # Source repo's working tree must NOT contain the marker file.
    from pathlib import Path as _P

    assert not (_P(repo) / "marker.txt").exists()
    # Persistence note must reflect the worktree push.
    assert "worktree branch" in result.persistence_note
    assert result.commit_sha is not None


@pytest.mark.asyncio
async def test_worktree_parallel_dispatches_do_not_cross_contaminate(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Any
) -> None:
    """Two parallel dispatches against the same cwd with isolation='worktree'
    must commit DISJOINT files — no cross-pollination via git add -A.
    """
    import asyncio

    repo = await _init_git_repo(tmp_path)

    class _WriteA(DispatcherBase):
        upstream_name = "wa"

        def cli_args(self, **kw: Any) -> list[str]:
            # Slow A so B can race in
            return [
                "/bin/sh",
                "-c",
                "echo A > only-A.txt; sleep 1; echo done >> only-A.txt",
            ]

    class _WriteB(DispatcherBase):
        upstream_name = "wb"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "echo B > only-B.txt"]

    da = _WriteA(circuits=reg, telemetry=tel, timeout_seconds=15)
    db = _WriteB(circuits=reg, telemetry=tel, timeout_seconds=15)
    ra, rb = await asyncio.gather(
        da.dispatch(
            task_id="t_par_a",
            cwd=repo,
            task="A",
            isolation="worktree",
            auto_commit=True,
        ),
        db.dispatch(
            task_id="t_par_b",
            cwd=repo,
            task="B",
            isolation="worktree",
            auto_commit=True,
        ),
    )
    assert ra.ok and rb.ok

    # Local branches are cleaned up after push; query the bare remote
    # directly to verify the pushed branches have disjoint trees.
    import subprocess
    from pathlib import Path as _P

    remote_dir = str(_P(repo).parent / "remote.git")
    out = subprocess.run(
        ["git", "--git-dir", remote_dir, "ls-tree", "-r", "--name-only", "fleet/t_par_a"],
        capture_output=True,
        text=True,
        check=False,
    )
    a_files = set(out.stdout.split())
    out = subprocess.run(
        ["git", "--git-dir", remote_dir, "ls-tree", "-r", "--name-only", "fleet/t_par_b"],
        capture_output=True,
        text=True,
        check=False,
    )
    b_files = set(out.stdout.split())
    # only-A.txt must be in A's branch and NOT in B's branch (the race fix).
    assert "only-A.txt" in a_files
    assert "only-A.txt" not in b_files
    assert "only-B.txt" in b_files
    assert "only-B.txt" not in a_files


@pytest.mark.asyncio
async def test_worktree_cleaned_up_after_success(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Any
) -> None:
    """The /tmp/fleet-worktrees/<task_id> dir is removed after a successful
    commit + push (no need to keep it around — branch is on origin)."""
    from pathlib import Path as _P

    repo = await _init_git_repo(tmp_path)

    class _Writer(DispatcherBase):
        upstream_name = "cleanup"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "echo cleanup > out.txt"]

    d = _Writer(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_iso_cleanup",
        cwd=repo,
        task="cleanup test",
        isolation="worktree",
        auto_commit=True,
    )
    assert result.ok is True
    # /tmp/fleet-worktrees/t_iso_cleanup should NOT exist after teardown.
    assert not _P("/tmp/fleet-worktrees/t_iso_cleanup").exists()


@pytest.mark.asyncio
async def test_worktree_fallback_when_cwd_not_git(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Any
) -> None:
    """isolation='worktree' against a non-git cwd falls back gracefully and
    runs in the original cwd (no hard fail)."""
    non_git = tmp_path / "nogit"
    non_git.mkdir()

    class _Writer(DispatcherBase):
        upstream_name = "fallback"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "echo nogit > marker.txt"]

    d = _Writer(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_iso_fallback",
        cwd=str(non_git),
        task="fallback",
        isolation="worktree",
        auto_commit=True,
    )
    assert result.ok is True
    # The marker landed in the non-git cwd directly (no worktree).
    assert (non_git / "marker.txt").exists()
    assert "not a git repo" in result.persistence_note or result.persistence_note == ""
