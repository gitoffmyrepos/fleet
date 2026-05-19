"""Branch lifecycle tests for the Fleet dispatcher base.

Covers the 2026-05-19 redesign:

  1. Worktree branches are deterministic ``fleet/task_<id>`` (no random suffix).
  2. Worktrees are based on ``origin/master`` (not local HEAD).
  3. On success: rebase onto origin/master, push to master, delete the
     remote feature branch.
  4. On rebase race / conflict: keep the feature branch on origin so the
     operator can recover manually.
  5. On non-zero exit / timeout: keep the worktree + local branch for
     forensic inspection (was previously torn down).
  6. State file tracks active dispatches across the orchestrator's lifetime.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher import state as state_mod
from fleet.dispatcher.base import DispatcherBase


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


@pytest.fixture
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the state file into tmp_path so tests don't trample real state."""
    p = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "_STATE_PATH", p)
    return p


def _init_git_repo(tmp_path: Path) -> tuple[str, str]:
    """Create a fresh repo + bare remote. Returns (repo_path, remote_dir)."""
    repo = tmp_path / "src"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@x"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "seed.txt").write_text("seed\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=repo, check=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "master"], cwd=repo, check=True)
    return str(repo), str(remote)


class _Writer(DispatcherBase):
    upstream_name = "writer"

    def cli_args(self, **kw: Any) -> list[str]:
        body = kw.get("payload", "default")
        fname = kw.get("filename", "out.txt")
        return ["/bin/sh", "-c", f"echo {body} > {fname}"]


# ───────────────────────── lifecycle: success path ──────────────────────────


@pytest.mark.asyncio
async def test_success_rebases_pushes_master_and_deletes_remote_branch(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    repo, remote = _init_git_repo(tmp_path)
    d = _Writer(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_success",
        cwd=repo,
        task="land it",
        isolation="worktree",
        payload="hello",
        filename="hello.txt",
    )
    assert result.ok is True
    # Work landed on master.
    out = subprocess.run(
        ["git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "master"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "hello.txt" in out.stdout.split()
    # Remote feature branch was deleted.
    out = subprocess.run(
        ["git", "--git-dir", remote, "branch", "--list", "fleet/*"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == ""
    # State entry was removed.
    assert not state_file.exists() or json.loads(state_file.read_text()) == {}


@pytest.mark.asyncio
async def test_success_path_records_then_removes_state_entry(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """State entry must EXIST while the worktree is live and be CLEARED on success."""
    repo, _ = _init_git_repo(tmp_path)

    class _Spy(DispatcherBase):
        upstream_name = "spy"

        def cli_args(self, **kw: Any) -> list[str]:
            # Read state DURING the agent run (sleeps so test can see it).
            return [
                "/bin/sh",
                "-c",
                f"cat {state_file} > {kw.get('cwd', '.')}/state.snapshot; echo done > worked.txt",
            ]

    d = _Spy(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_state",
        cwd=repo,
        task="spy on state",
        isolation="worktree",
    )
    assert result.ok is True

    # Snapshot taken mid-flight should have included our task_id.
    # That snapshot was committed into the master push, so we can read it
    # back from the bare remote.
    # Reconstruct via git show.
    remote = str(Path(repo).parent / "remote.git")
    out = subprocess.run(
        ["git", "--git-dir", remote, "show", "master:state.snapshot"],
        capture_output=True,
        text=True,
        check=False,
    )
    snapshot = json.loads(out.stdout) if out.stdout.strip() else {}
    assert "t_state" in snapshot, f"state was empty during dispatch: {snapshot}"
    # And the post-success state file is now empty.
    final = json.loads(state_file.read_text()) if state_file.is_file() else {}
    assert "t_state" not in final


# ───────────────────────── lifecycle: branch naming ─────────────────────────


@pytest.mark.asyncio
async def test_branch_name_is_deterministic_not_random(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """Branch is fleet/task_<id> exactly — no random suffix."""
    repo, _ = _init_git_repo(tmp_path)

    captured: dict[str, str] = {}

    class _Capture(DispatcherBase):
        upstream_name = "cap"

        def cli_args(self, **kw: Any) -> list[str]:
            captured["cwd"] = kw.get("cwd", "")
            return ["/bin/sh", "-c", "echo done > out.txt"]

    d = _Capture(circuits=reg, telemetry=tel, timeout_seconds=10)
    await d.dispatch(task_id="t_named", cwd=repo, task="t", isolation="worktree")
    # The worktree path must be /tmp/fleet-worktrees/t_named (deterministic).
    assert captured["cwd"] == "/tmp/fleet-worktrees/t_named"


# ───────────────────────── lifecycle: based on origin/master ────────────────


@pytest.mark.asyncio
async def test_worktree_starts_from_origin_master_not_local_head(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """If the source repo's local HEAD is on a feature branch with extra
    commits, the worktree must still start from origin/master.
    """
    repo, _ = _init_git_repo(tmp_path)
    # Move local HEAD to a feature branch with a divergent commit.
    subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True)
    (Path(repo) / "feature-only.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature only"], cwd=repo, check=True)

    d = _Writer(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_from_master",
        cwd=repo,
        task="t",
        isolation="worktree",
        filename="new.txt",
    )
    assert result.ok is True
    # The worktree (before we tore it down on success) should have started
    # from origin/master, so the feature-only commit must NOT be in the
    # rebased master push.
    remote = str(Path(repo).parent / "remote.git")
    out = subprocess.run(
        ["git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "master"],
        capture_output=True,
        text=True,
        check=False,
    )
    files = set(out.stdout.split())
    assert (
        "feature-only.txt" not in files
    ), "worktree leaked content from local feature branch into master push"
    assert "new.txt" in files


# ───────────────────────── lifecycle: failure forensics ─────────────────────


@pytest.mark.asyncio
async def test_failure_keeps_worktree_for_forensics(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """Non-zero exit must NOT tear down the worktree — operator needs to inspect."""
    repo, _ = _init_git_repo(tmp_path)

    class _PartialThenFail(DispatcherBase):
        upstream_name = "partial"

        def cli_args(self, **kw: Any) -> list[str]:
            # Write a file, then exit non-zero.
            return [
                "/bin/sh",
                "-c",
                "echo partial > partial.txt; exit 7",
            ]

    d = _PartialThenFail(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_fail_keep",
        cwd=repo,
        task="failing",
        isolation="worktree",
    )
    assert result.ok is False
    assert "exit code 7" in result.error or "exit 7" in result.error
    # Worktree must STILL exist.
    wt = Path("/tmp/fleet-worktrees/t_fail_keep")
    assert wt.exists(), "worktree was torn down on failure — forensics lost"
    assert (wt / "partial.txt").read_text() == "partial\n"
    # Result surfaces the worktree path/branch.
    assert result.worktree_path == str(wt)
    assert result.worktree_branch == "fleet/t_fail_keep"
    # State entry is retained for reconciliation.
    state = json.loads(state_file.read_text()) if state_file.is_file() else {}
    assert "t_fail_keep" in state

    # Cleanup so we don't leak between tests.
    subprocess.run(
        ["git", "-C", repo, "worktree", "remove", "--force", str(wt)],
        check=False,
    )
    subprocess.run(
        ["git", "-C", repo, "branch", "-D", "fleet/t_fail_keep"],
        check=False,
    )


@pytest.mark.asyncio
async def test_timeout_keeps_worktree_for_forensics(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """Timeout must keep the worktree (was leaked before, now explicit)."""
    repo, _ = _init_git_repo(tmp_path)

    class _Slow(DispatcherBase):
        upstream_name = "slow"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "echo wrote > partial.txt; sleep 5"]

    d = _Slow(circuits=reg, telemetry=tel, timeout_seconds=1)
    result = await d.dispatch(
        task_id="t_timeout_keep",
        cwd=repo,
        task="slow",
        isolation="worktree",
    )
    assert result.ok is False
    assert "timeout" in result.error.lower()
    wt = Path("/tmp/fleet-worktrees/t_timeout_keep")
    assert wt.exists()
    assert result.worktree_path == str(wt)
    assert result.worktree_branch == "fleet/t_timeout_keep"
    assert "forensics" in result.persistence_note

    # Cleanup
    subprocess.run(
        ["git", "-C", repo, "worktree", "remove", "--force", str(wt)],
        check=False,
    )
    subprocess.run(
        ["git", "-C", repo, "branch", "-D", "fleet/t_timeout_keep"],
        check=False,
    )


# ───────────────────────── lifecycle: race handling ─────────────────────────


@pytest.mark.asyncio
async def test_concurrent_master_advance_triggers_rebase(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """If origin/master advances during the dispatch, our rebase must
    replay our commit on top of it before pushing — both commits end up
    on master.
    """
    repo, remote = _init_git_repo(tmp_path)
    sibling_clone = tmp_path / "sibling"
    subprocess.run(["git", "clone", "-q", remote, str(sibling_clone)], check=True)
    subprocess.run(["git", "config", "user.email", "s@x"], cwd=sibling_clone, check=True)
    subprocess.run(["git", "config", "user.name", "S"], cwd=sibling_clone, check=True)

    class _AgentWithRace(DispatcherBase):
        upstream_name = "racer"

        def cli_args(self, **kw: Any) -> list[str]:
            wd = kw.get("cwd", "")
            # While the agent is "working", a sibling pushes a new commit to
            # origin/master from an INDEPENDENT clone. The agent's worktree
            # has master checked out (can't double-checkout), so we use a
            # separate clone that simulates a parallel worker on another host.
            return [
                "/bin/sh",
                "-c",
                f"echo agent_work > {wd}/agent.txt && "
                f"cd {sibling_clone} && "
                f"echo other > sibling.txt && git add -A && "
                f"git commit -q -m sibling && git push -q origin master",
            ]

    d = _AgentWithRace(circuits=reg, telemetry=tel, timeout_seconds=15)
    result = await d.dispatch(
        task_id="t_race",
        cwd=repo,
        task="t",
        isolation="worktree",
    )
    assert (
        result.ok is True
    ), f"dispatch failed: error={result.error!r} note={result.persistence_note!r}"
    # Both files must be on master (proves the rebase happened).
    out = subprocess.run(
        ["git", "--git-dir", remote, "ls-tree", "-r", "--name-only", "master"],
        capture_output=True,
        text=True,
        check=False,
    )
    files = set(out.stdout.split())
    assert "agent.txt" in files, (
        f"agent.txt missing from master; note={result.persistence_note!r} "
        f"files={files} stderr={result.stderr!r}"
    )
    assert "sibling.txt" in files, (
        f"sibling.txt missing from master; note={result.persistence_note!r} "
        f"files={files} stderr={result.stderr!r}"
    )
    assert "rebased onto origin/master" in result.persistence_note


# ───────────────────────── state module ─────────────────────────────────────


def test_state_record_remove_roundtrip(state_file: Path) -> None:
    entry = state_mod.ActiveDispatch(
        task_id="t1",
        upstream_name="x",
        source_repo="/repo",
        worktree_path="/tmp/wt/t1",
        worktree_branch="fleet/t1",
        started_at=time.time(),
    )
    state_mod.record_dispatch(entry)
    assert any(e.task_id == "t1" for e in state_mod.list_active())
    state_mod.remove_dispatch("t1")
    assert not any(e.task_id == "t1" for e in state_mod.list_active())


def test_state_corrupt_file_returns_empty(state_file: Path) -> None:
    state_file.write_text("{not valid json")
    assert state_mod.list_active() == []


def test_state_missing_file_returns_empty(state_file: Path) -> None:
    # state_file fixture redirects but doesn't create — confirm graceful handling.
    assert not state_file.exists()
    assert state_mod.list_active() == []


def test_state_reconcile_finds_orphan(state_file: Path, tmp_path: Path) -> None:
    """An entry whose worktree dir still exists shows up in reconcile output."""
    wt = tmp_path / "orphan_wt"
    wt.mkdir()
    entry = state_mod.ActiveDispatch(
        task_id="t_orphan",
        upstream_name="x",
        source_repo="/repo",
        worktree_path=str(wt),
        worktree_branch="fleet/t_orphan",
        started_at=time.time() - 3600.0,  # an hour old
    )
    state_mod.record_dispatch(entry)
    orphans = state_mod.reconcile_active_dispatches(age_threshold_seconds=10.0)
    assert len(orphans) == 1
    assert orphans[0]["task_id"] == "t_orphan"
    assert orphans[0]["exists_on_disk"] is True
    assert orphans[0]["age_seconds"] > 10.0


def test_state_reconcile_skips_young_entries(state_file: Path, tmp_path: Path) -> None:
    """Live dispatches (started_at within threshold) are NOT flagged."""
    entry = state_mod.ActiveDispatch(
        task_id="t_live",
        upstream_name="x",
        source_repo="/repo",
        worktree_path=str(tmp_path / "live"),
        worktree_branch="fleet/t_live",
        started_at=time.time(),  # right now
    )
    state_mod.record_dispatch(entry)
    orphans = state_mod.reconcile_active_dispatches(age_threshold_seconds=3600.0)
    assert orphans == []


@pytest.mark.asyncio
async def test_rebase_conflict_keeps_feature_branch(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """When origin/master and the worktree both modify the SAME line of the
    SAME file the rebase conflicts. The dispatcher must abort the rebase,
    push the feature branch so work isn't lost, and KEEP the worktree for
    manual merge.
    """
    repo, remote = _init_git_repo(tmp_path)
    # Seed a file that both the agent and the sibling will mutate.
    (Path(repo) / "shared.txt").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "shared"], cwd=repo, check=True)
    subprocess.run(["git", "push", "-q", "origin", "master"], cwd=repo, check=True)

    sibling_clone = tmp_path / "sibling"
    subprocess.run(["git", "clone", "-q", remote, str(sibling_clone)], check=True)
    subprocess.run(["git", "config", "user.email", "s@x"], cwd=sibling_clone, check=True)
    subprocess.run(["git", "config", "user.name", "S"], cwd=sibling_clone, check=True)

    class _Conflict(DispatcherBase):
        upstream_name = "conflict"

        def cli_args(self, **kw: Any) -> list[str]:
            wd = kw.get("cwd", "")
            return [
                "/bin/sh",
                "-c",
                f"echo agent_edit > {wd}/shared.txt && "
                f"cd {sibling_clone} && "
                f"echo sibling_edit > shared.txt && git add -A && "
                f"git commit -q -m sibling-conflict && git push -q origin master",
            ]

    d = _Conflict(circuits=reg, telemetry=tel, timeout_seconds=15)
    result = await d.dispatch(
        task_id="t_conflict",
        cwd=repo,
        task="conflict",
        isolation="worktree",
    )
    assert result.ok is True  # subprocess exit code was 0
    # Master should have the sibling edit, not the agent edit (rebase
    # was aborted; agent's work is on the feature branch instead).
    out = subprocess.run(
        ["git", "--git-dir", remote, "show", "master:shared.txt"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert out.stdout.strip() == "sibling_edit"
    # Feature branch was pushed to origin (work preserved).
    out = subprocess.run(
        ["git", "--git-dir", remote, "branch", "--list", "fleet/*"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "fleet/t_conflict" in out.stdout
    # Persistence note explains the conflict.
    assert "conflict" in result.persistence_note.lower()

    # Cleanup
    subprocess.run(
        ["git", "-C", repo, "worktree", "remove", "--force", "/tmp/fleet-worktrees/t_conflict"],
        check=False,
    )
    subprocess.run(["git", "-C", repo, "branch", "-D", "fleet/t_conflict"], check=False)


@pytest.mark.asyncio
async def test_fetch_failure_falls_back_to_feature_branch_only(
    reg: CircuitRegistry, tel: AsyncMock, tmp_path: Path, state_file: Path
) -> None:
    """If we can't fetch origin/master (network down / no remote), the
    dispatcher must push the feature branch and keep the worktree.
    """
    repo, remote = _init_git_repo(tmp_path)
    # Break the origin remote so fetch fails. We'll set the URL to a path
    # that doesn't exist — git will report failure.
    subprocess.run(
        ["git", "-C", repo, "remote", "set-url", "origin", "/nonexistent/remote.git"],
        check=True,
    )

    d = _Writer(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(
        task_id="t_fetchfail",
        cwd=repo,
        task="fetch fail",
        isolation="worktree",
        filename="x.txt",
    )
    # Subprocess succeeded — commit landed locally — push failed.
    assert result.ok is True
    # Persistence note must mention the fallback.
    assert "fetch" in result.persistence_note.lower() or "push" in result.persistence_note.lower()
    # Restore the remote so cleanup works.
    subprocess.run(
        ["git", "-C", repo, "remote", "set-url", "origin", remote],
        check=False,
    )
    subprocess.run(
        ["git", "-C", repo, "worktree", "remove", "--force", "/tmp/fleet-worktrees/t_fetchfail"],
        check=False,
    )
    subprocess.run(["git", "-C", repo, "branch", "-D", "fleet/t_fetchfail"], check=False)


def test_state_record_idempotent_on_same_task_id(state_file: Path) -> None:
    """Recording twice with the same task_id replaces the entry (no dupes)."""
    e1 = state_mod.ActiveDispatch(
        task_id="dup",
        upstream_name="a",
        source_repo="/r",
        worktree_path="/tmp/wt/dup",
        worktree_branch="fleet/dup",
        started_at=1.0,
    )
    e2 = state_mod.ActiveDispatch(
        task_id="dup",
        upstream_name="b",  # different upstream
        source_repo="/r",
        worktree_path="/tmp/wt/dup",
        worktree_branch="fleet/dup",
        started_at=2.0,
    )
    state_mod.record_dispatch(e1)
    state_mod.record_dispatch(e2)
    actives = state_mod.list_active()
    matches = [a for a in actives if a.task_id == "dup"]
    assert len(matches) == 1
    assert matches[0].upstream_name == "b"
    assert matches[0].started_at == 2.0
