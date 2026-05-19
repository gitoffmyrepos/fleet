"""Unit tests for fleet.reconciler.

We mock git via monkeypatching ``_run_git`` and the state-file loader,
so these tests run hermetically — no real worktrees touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from fleet import reconciler
from fleet.reconciler import (
    ReconcileReport,
    WorktreeReport,
    _classify_worktree,
    _extract_task_id,
    _is_fleet_managed,
    _load_active_dispatches,
    _summarize,
    reconcile,
)

# ─── Helpers ────────────────────────────────────────────────────────────────


def _git_responses(*responses: tuple[int, str, str]) -> Any:
    """Build a side_effect callable that yields the given tuples in order.

    Each tuple is ``(returncode, stdout, stderr)``. Extra calls raise so
    over-mocking is loud.
    """
    it = iter(responses)

    def _resp(*args: Any, **kwargs: Any) -> tuple[int, str, str]:
        try:
            return next(it)
        except StopIteration as e:
            raise AssertionError(
                f"unexpected extra _run_git call: args={args} kwargs={kwargs}"
            ) from e

    return _resp


# ─── Pattern detection ──────────────────────────────────────────────────────


@pytest.mark.unit
def test_is_fleet_managed_matches_fleet_worktree_root() -> None:
    assert _is_fleet_managed("/tmp/fleet-worktrees/task_abc123/")
    assert _is_fleet_managed("/tmp/fleet-worktrees/task_abc123")


@pytest.mark.unit
def test_is_fleet_managed_matches_fx_prefix() -> None:
    assert _is_fleet_managed("/tmp/fx-some-suffix")


@pytest.mark.unit
def test_is_fleet_managed_rejects_unrelated_paths() -> None:
    assert not _is_fleet_managed("/home/kelvin/SB-HomeLAb/FX")
    assert not _is_fleet_managed("/tmp/random-dir")
    assert not _is_fleet_managed("/tmp/fleet-worktrees")  # no trailing slash + task


@pytest.mark.unit
def test_extract_task_id_for_fleet_pattern() -> None:
    assert _extract_task_id("/tmp/fleet-worktrees/task_abc123") == "task_abc123"
    assert _extract_task_id("/tmp/fleet-worktrees/task_abc123/") == "task_abc123"


@pytest.mark.unit
def test_extract_task_id_for_fx_pattern() -> None:
    assert _extract_task_id("/tmp/fx-blah") == "fx-blah"


@pytest.mark.unit
def test_extract_task_id_returns_none_for_random_path() -> None:
    assert _extract_task_id("/tmp/random") is None


# ─── State file parsing ─────────────────────────────────────────────────────


@pytest.mark.unit
def test_load_active_dispatches_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "missing.json"
    active, present = _load_active_dispatches(p)
    assert active == set()
    assert present is False


@pytest.mark.unit
def test_load_active_dispatches_array_format(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            [
                {"task_id": "task_aaa", "status": "ACTIVE"},
                {"task_id": "task_bbb", "status": "SUCCEEDED"},
                {"task_id": "task_ccc", "status": "FAILED"},
                {"task_id": "task_ddd"},  # no status → defaults ACTIVE
            ]
        )
    )
    active, present = _load_active_dispatches(p)
    assert active == {"task_aaa", "task_ddd"}
    assert present is True


@pytest.mark.unit
def test_load_active_dispatches_dict_format(tmp_path: Path) -> None:
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {
                "task_aaa": {"status": "ACTIVE"},
                "task_bbb": {"status": "SUCCEEDED"},
            }
        )
    )
    active, _ = _load_active_dispatches(p)
    assert active == {"task_aaa"}


@pytest.mark.unit
def test_load_active_dispatches_malformed_json(tmp_path: Path) -> None:
    """A corrupted state file MUST NOT cause destructive action — we
    return an empty active set but mark the file as present so the
    caller sees the warning."""
    p = tmp_path / "state.json"
    p.write_text("{ this is not JSON")
    active, present = _load_active_dispatches(p)
    assert active == set()
    assert present is True  # file existed, was just unparseable


@pytest.mark.unit
def test_load_active_dispatches_ignores_rows_without_task_id(tmp_path: Path) -> None:
    """Defensive: rows missing task_id are skipped, not crashed on."""
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            [
                {"status": "ACTIVE"},  # missing task_id
                {"task_id": "task_real", "status": "ACTIVE"},
                "not a dict",
            ]
        )
    )
    active, _ = _load_active_dispatches(p)
    assert active == {"task_real"}


# ─── Classification ─────────────────────────────────────────────────────────


@pytest.mark.unit
def test_classify_active_dispatch_is_left_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A worktree whose task_id is ACTIVE in state MUST be skipped — we
    never touch in-flight work."""
    # Should not call git at all for an active worktree
    monkeypatch.setattr(
        reconciler,
        "_run_git",
        MagicMock(side_effect=AssertionError("git must not be called for ACTIVE")),
    )
    wr = _classify_worktree(
        repo="/repo",
        worktree_path="/tmp/fleet-worktrees/task_live",
        branch="fleet/task_live",
        active_task_ids={"task_live"},
        stale_threshold_seconds=7 * 86400,
    )
    assert wr.verdict == "active"


@pytest.mark.unit
def test_classify_merged_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Branch that's a clean ancestor of origin/master → MERGED."""
    # 1. rev-parse origin/master → 0
    # 2. merge-base --is-ancestor → 0
    # 3. log -1 --format=%ct → 0 + timestamp
    monkeypatch.setattr(
        reconciler,
        "_run_git",
        MagicMock(
            side_effect=_git_responses(
                (0, "deadbeef\n", ""),  # rev-parse origin/master
                (0, "", ""),  # merge-base is-ancestor (rc=0 → merged)
                (0, "1234567890\n", ""),  # log %ct
            )
        ),
    )
    wr = _classify_worktree(
        repo="/repo",
        worktree_path="/tmp/fleet-worktrees/task_done",
        branch="fleet/task_done",
        active_task_ids=set(),
        stale_threshold_seconds=7 * 86400,
    )
    assert wr.verdict == "merged"
    assert "ancestor" in wr.reason


@pytest.mark.unit
def test_classify_stale_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not merged + last commit > stale_days → STALE."""
    import time

    old_ts = int(time.time()) - (30 * 86400)  # 30 days old
    monkeypatch.setattr(
        reconciler,
        "_run_git",
        MagicMock(
            side_effect=_git_responses(
                (0, "deadbeef\n", ""),  # rev-parse origin/master
                (1, "", "not ancestor"),  # merge-base is-ancestor (rc=1)
                (0, f"{old_ts}\n", ""),  # log %ct
            )
        ),
    )
    wr = _classify_worktree(
        repo="/repo",
        worktree_path="/tmp/fleet-worktrees/task_old",
        branch="fleet/task_old",
        active_task_ids=set(),
        stale_threshold_seconds=7 * 86400,
    )
    assert wr.verdict == "stale"
    assert wr.last_commit_age_seconds is not None
    assert wr.last_commit_age_seconds >= 7 * 86400


@pytest.mark.unit
def test_classify_unknown_when_not_merged_and_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    """Not merged + fresh commit → UNKNOWN (don't touch)."""
    import time

    fresh_ts = int(time.time()) - (2 * 86400)  # 2 days
    monkeypatch.setattr(
        reconciler,
        "_run_git",
        MagicMock(
            side_effect=_git_responses(
                (0, "deadbeef\n", ""),  # rev-parse origin/master
                (1, "", "not ancestor"),  # merge-base is-ancestor
                (0, f"{fresh_ts}\n", ""),  # log %ct
            )
        ),
    )
    wr = _classify_worktree(
        repo="/repo",
        worktree_path="/tmp/fleet-worktrees/task_recent",
        branch="fleet/task_recent",
        active_task_ids=set(),
        stale_threshold_seconds=7 * 86400,
    )
    assert wr.verdict == "unknown"


@pytest.mark.unit
def test_classify_detached_head_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """No branch attached → can't reason about merge state → UNKNOWN."""
    # git should not be called when there's no branch
    monkeypatch.setattr(
        reconciler,
        "_run_git",
        MagicMock(side_effect=AssertionError("git must not be called for detached")),
    )
    wr = _classify_worktree(
        repo="/repo",
        worktree_path="/tmp/fleet-worktrees/task_detached",
        branch=None,
        active_task_ids=set(),
        stale_threshold_seconds=7 * 86400,
    )
    assert wr.verdict == "unknown"


# ─── End-to-end reconcile() ─────────────────────────────────────────────────


@pytest.mark.unit
def test_reconcile_dry_run_does_not_delete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Dry-run mode (default) classifies but never deletes — even on MERGED."""
    # Set up: one repo with one MERGED worktree.
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))
    report_path = tmp_path / "report.json"

    # Mock git operations + worktree list
    monkeypatch.setattr(
        reconciler,
        "_list_fleet_worktrees",
        lambda repo: [("/tmp/fleet-worktrees/task_merged", "fleet/task_merged")],
    )
    monkeypatch.setattr(
        reconciler,
        "_branch_merged_to_master",
        lambda repo, branch: (True, "ancestor of origin/master"),
    )
    monkeypatch.setattr(
        reconciler,
        "_branch_last_commit_age_seconds",
        lambda repo, branch: 3600,
    )

    delete_calls: list[Any] = []
    monkeypatch.setattr(
        reconciler,
        "_delete_worktree",
        lambda *a, **kw: delete_calls.append((a, kw)) or None,
    )

    report = reconcile(
        repos=[str(repo_dir)],
        state_file=state_file,
        report_path=report_path,
        apply=False,
    )
    assert report.dry_run is True
    assert len(report.worktrees) == 1
    assert report.worktrees[0].verdict == "merged"
    assert report.worktrees[0].action_taken == "none"
    assert delete_calls == [], "dry-run must not delete anything"
    # Report persisted to disk
    assert report_path.exists()
    on_disk = json.loads(report_path.read_text())
    assert on_disk["dry_run"] is True


@pytest.mark.unit
def test_reconcile_apply_deletes_merged_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--apply deletes MERGED worktrees but leaves STALE+UNKNOWN alone
    unless --stale-apply is also set."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))

    monkeypatch.setattr(
        reconciler,
        "_list_fleet_worktrees",
        lambda repo: [
            ("/tmp/fleet-worktrees/task_merged", "fleet/task_merged"),
            ("/tmp/fleet-worktrees/task_stale", "fleet/task_stale"),
            ("/tmp/fleet-worktrees/task_unknown", "fleet/task_unknown"),
        ],
    )
    # Each branch gets a different verdict baked in. Ages are in
    # seconds-since-last-commit, NOT timestamps.
    fresh_age = 3600  # 1h
    old_age = 30 * 86400  # 30d

    def _merged(repo: str, branch: str) -> tuple[bool, str]:
        return (branch == "fleet/task_merged", "ok")

    def _age(repo: str, branch: str) -> int | None:
        return {
            "fleet/task_merged": fresh_age,
            "fleet/task_stale": old_age,
            "fleet/task_unknown": fresh_age,
        }[branch]

    monkeypatch.setattr(reconciler, "_branch_merged_to_master", _merged)
    monkeypatch.setattr(reconciler, "_branch_last_commit_age_seconds", _age)

    delete_calls: list[tuple[str, str, str | None]] = []

    def _capture_delete(repo: str, worktree_path: str, branch: str | None) -> None:
        delete_calls.append((repo, worktree_path, branch))
        return None

    monkeypatch.setattr(reconciler, "_delete_worktree", _capture_delete)

    report = reconcile(
        repos=[str(repo_dir)],
        state_file=state_file,
        report_path=None,
        apply=True,
        stale_apply=False,
    )
    # Verify classifications
    verdicts = {wr.branch: wr.verdict for wr in report.worktrees}
    assert verdicts == {
        "fleet/task_merged": "merged",
        "fleet/task_stale": "stale",
        "fleet/task_unknown": "unknown",
    }
    # Only the merged worktree should be deleted
    assert len(delete_calls) == 1
    assert delete_calls[0][1] == "/tmp/fleet-worktrees/task_merged"
    # Stale + unknown left alone
    actions = {wr.branch: wr.action_taken for wr in report.worktrees}
    assert actions["fleet/task_merged"] == "deleted"
    assert actions["fleet/task_stale"] == "none"
    assert actions["fleet/task_unknown"] == "skipped"


@pytest.mark.unit
def test_reconcile_apply_with_stale_apply_deletes_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When --stale-apply is set, STALE worktrees ARE deleted."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))

    old_age = 30 * 86400  # 30 days in seconds

    monkeypatch.setattr(
        reconciler,
        "_list_fleet_worktrees",
        lambda repo: [("/tmp/fleet-worktrees/task_stale", "fleet/task_stale")],
    )
    monkeypatch.setattr(reconciler, "_branch_merged_to_master", lambda *a: (False, "x"))
    monkeypatch.setattr(reconciler, "_branch_last_commit_age_seconds", lambda *a: old_age)

    deletes: list[Any] = []
    monkeypatch.setattr(
        reconciler,
        "_delete_worktree",
        lambda *a, **kw: deletes.append((a, kw)) or None,
    )
    report = reconcile(
        repos=[str(repo_dir)],
        state_file=state_file,
        report_path=None,
        apply=True,
        stale_apply=True,
    )
    assert len(deletes) == 1
    assert report.worktrees[0].action_taken == "deleted"


@pytest.mark.unit
def test_reconcile_skips_active_worktrees_in_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ACTIVE worktree in state is NEVER touched, even with --apply."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([{"task_id": "task_live", "status": "ACTIVE"}]))

    monkeypatch.setattr(
        reconciler,
        "_list_fleet_worktrees",
        lambda repo: [("/tmp/fleet-worktrees/task_live", "fleet/task_live")],
    )
    # git ops must NOT be called for active worktrees
    monkeypatch.setattr(
        reconciler,
        "_branch_merged_to_master",
        lambda *a: pytest.fail("must not check merge for active"),
    )
    monkeypatch.setattr(
        reconciler,
        "_delete_worktree",
        lambda *a, **kw: pytest.fail("must not delete active"),
    )
    report = reconcile(
        repos=[str(repo_dir)],
        state_file=state_file,
        report_path=None,
        apply=True,
        stale_apply=True,
    )
    assert report.worktrees[0].verdict == "active"
    assert report.worktrees[0].action_taken == "skipped"


@pytest.mark.unit
def test_reconcile_handles_missing_state_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If state file is missing, default to fail-safe behaviour: every
    worktree gets classified normally, but we mark state_file_present
    so the report makes the gap visible."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    state_file = tmp_path / "does-not-exist.json"

    monkeypatch.setattr(reconciler, "_list_fleet_worktrees", lambda repo: [])
    report = reconcile(
        repos=[str(repo_dir)],
        state_file=state_file,
        report_path=None,
        apply=False,
    )
    assert report.state_file_present is False
    assert report.active_task_ids == []


@pytest.mark.unit
def test_reconcile_skips_nonexistent_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo path that doesn't exist is silently skipped, not crashed on."""
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))
    monkeypatch.setattr(
        reconciler,
        "_list_fleet_worktrees",
        lambda repo: pytest.fail("must not list nonexistent repo"),
    )
    report = reconcile(
        repos=["/tmp/does-not-exist-fleet-test"],
        state_file=state_file,
        report_path=None,
        apply=False,
    )
    assert report.worktrees == []


@pytest.mark.unit
def test_summarize_counts_by_verdict_and_action() -> None:
    rows = [
        WorktreeReport("r", "p1", "b1", "merged", "x", action_taken="deleted"),
        WorktreeReport("r", "p2", "b2", "merged", "x", action_taken="none"),
        WorktreeReport("r", "p3", "b3", "stale", "x", action_taken="none"),
        WorktreeReport("r", "p4", "b4", "active", "x", action_taken="skipped"),
        WorktreeReport("r", "p5", "b5", "unknown", "x", action_taken="error", error="boom"),
    ]
    s = _summarize(rows)
    assert s["total"] == 5
    assert s["merged"] == 2
    assert s["stale"] == 1
    assert s["active"] == 1
    assert s["unknown"] == 1
    assert s["deleted"] == 1
    assert s["errors"] == 1


# ─── CLI ────────────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_cli_dry_run_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI without --apply runs in dry-run mode."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))
    report_path = tmp_path / "report.json"

    monkeypatch.setattr(reconciler, "_list_fleet_worktrees", lambda r: [])

    rc = reconciler.main(
        [
            "--repo",
            str(repo),
            "--state-file",
            str(state_file),
            "--report-path",
            str(report_path),
            "--quiet",
        ]
    )
    assert rc == 0
    assert report_path.exists()
    on_disk = json.loads(report_path.read_text())
    assert on_disk["dry_run"] is True


@pytest.mark.unit
def test_cli_apply_flag_forwards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--apply flag flips dry_run to False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))
    report_path = tmp_path / "report.json"
    monkeypatch.setattr(reconciler, "_list_fleet_worktrees", lambda r: [])

    rc = reconciler.main(
        [
            "--apply",
            "--repo",
            str(repo),
            "--state-file",
            str(state_file),
            "--report-path",
            str(report_path),
            "--quiet",
        ]
    )
    assert rc == 0
    on_disk = json.loads(report_path.read_text())
    assert on_disk["dry_run"] is False


@pytest.mark.unit
def test_cli_exit_code_2_when_orphans_in_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run with stale/unknown findings exits 2 to signal 'review needed'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))
    report_path = tmp_path / "report.json"

    monkeypatch.setattr(
        reconciler,
        "_list_fleet_worktrees",
        lambda r: [("/tmp/fleet-worktrees/task_old", "fleet/task_old")],
    )
    monkeypatch.setattr(reconciler, "_branch_merged_to_master", lambda *a: (False, "x"))
    # Returns AGE in seconds (not a timestamp). 30 days = stale.
    monkeypatch.setattr(
        reconciler,
        "_branch_last_commit_age_seconds",
        lambda *a: 30 * 86400,
    )

    rc = reconciler.main(
        [
            "--repo",
            str(repo),
            "--state-file",
            str(state_file),
            "--report-path",
            str(report_path),
            "--quiet",
        ]
    )
    assert rc == 2


# ─── Metrics integration smoke ──────────────────────────────────────────────


@pytest.mark.unit
def test_record_metrics_is_best_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Metric facade may not be importable; reconciler must not crash."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps([]))
    monkeypatch.setattr(reconciler, "_list_fleet_worktrees", lambda r: [])
    # Patch the metrics import to fail
    with patch.dict("sys.modules", {"fleet.metrics": None}):
        report = reconcile(
            repos=[str(repo)],
            state_file=state_file,
            report_path=None,
            apply=False,
        )
    assert isinstance(report, ReconcileReport)
