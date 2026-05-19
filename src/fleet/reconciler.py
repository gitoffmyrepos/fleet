"""Orphan worktree/branch reconciler for Fleet.

A2 cleanup deliverable (2026-05-19) — defense-in-depth cleanup that
ensures Fleet stays clean even if A1's per-dispatch lifecycle fails
(crash, network glitch, kill -9, etc.).

Scope
-----

For each configured repo:

1. List worktrees matching Fleet patterns (``/tmp/fleet-worktrees/task_*``
   or ``/tmp/fx-*``).
2. For each Fleet-managed worktree, classify it:

   * ACTIVE — present in ``~/.local/state/fleet/active_dispatches.json``
     with status="ACTIVE". Skip entirely; do not touch.
   * MERGED — branch is fully merged into origin/master. Safe to
     delete worktree + branch.
   * STALE — branch's last commit is older than 7 days. Logged to the
     report and only deleted when ``--apply`` is passed.
   * UNKNOWN — anything we can't determine. Fail-safe: do nothing
     destructive. Log as needs-review.

3. Write a JSON report to ``/tmp/fleet-reconciler-report.json``.

CLI
---

::

    fleet-reconciler                    # dry-run, report only
    fleet-reconciler --apply            # actually delete classified orphans
    fleet-reconciler --repo /path/to/x  # add a repo to the scan list
    fleet-reconciler --stale-days 14    # override stale threshold
    fleet-reconciler --json             # emit the report to stdout too

Environment variables
---------------------

``FLEET_RECONCILER_REPOS``
    Colon-separated list of repos to scan. Defaults to FX +
    sb-gitops + sb-dev-infra.

``FLEET_RECONCILER_STATE_FILE``
    Path to A1's active-dispatches state file. Default
    ``~/.local/state/fleet/active_dispatches.json``.

``FLEET_RECONCILER_REPORT_PATH``
    Where to write the JSON report. Default
    ``/tmp/fleet-reconciler-report.json``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger("fleet.reconciler")

# ─── Defaults ───────────────────────────────────────────────────────────────

_DEFAULT_REPOS: tuple[str, ...] = (
    "/home/kelvin/SB-HomeLAb/FX",
    "/home/kelvin/SB-HomeLAb/sb-gitops",
    "/home/kelvin/SB-HomeLAb/sb-dev-infra",
)
_DEFAULT_STATE_FILE = Path.home() / ".local" / "state" / "fleet" / "active_dispatches.json"
_DEFAULT_REPORT_PATH = Path("/tmp/fleet-reconciler-report.json")
_FLEET_WORKTREE_ROOT = Path("/tmp/fleet-worktrees")
_FX_WORKTREE_PREFIX = "fx-"  # /tmp/fx-* pattern from older runs
_DEFAULT_STALE_DAYS = 7


# ─── Result types ───────────────────────────────────────────────────────────

# Classification verdicts. ACTIVE/UNKNOWN are never deleted; MERGED is
# safe to delete unconditionally; STALE requires --apply.
_VERDICT_ACTIVE = "active"
_VERDICT_MERGED = "merged"
_VERDICT_STALE = "stale"
_VERDICT_UNKNOWN = "unknown"


@dataclass
class WorktreeReport:
    """Per-worktree classification + action taken."""

    repo: str
    worktree_path: str
    branch: str | None
    verdict: str
    reason: str
    last_commit_age_seconds: int | None = None
    action_taken: str = "none"  # "deleted" | "skipped" | "none"
    error: str | None = None


@dataclass
class ReconcileReport:
    """Full report shape — what we write to ``--report-path``."""

    generated_at: str
    dry_run: bool
    stale_days: int
    repos_scanned: list[str]
    state_file: str
    state_file_present: bool
    active_task_ids: list[str]
    worktrees: list[WorktreeReport] = field(default_factory=list)
    summary: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "dry_run": self.dry_run,
            "stale_days": self.stale_days,
            "repos_scanned": self.repos_scanned,
            "state_file": self.state_file,
            "state_file_present": self.state_file_present,
            "active_task_ids": self.active_task_ids,
            "worktrees": [asdict(w) for w in self.worktrees],
            "summary": self.summary,
        }


# ─── Core helpers ───────────────────────────────────────────────────────────


def _load_active_dispatches(path: Path) -> tuple[set[str], bool]:
    """Read A1's state file and return ``(active_task_ids, file_was_present)``.

    Defensive parsing: if the file is missing, empty, or malformed we
    return ``set()`` and let the caller treat every worktree as
    unknown-state (which prevents destructive action on the orphan
    path).

    Schema assumption (per spec): a JSON array of objects each carrying
    ``task_id`` and ``status``. Unknown fields are ignored. Objects
    without a ``task_id`` are skipped silently.
    """
    if not path.exists():
        return set(), False
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("could not parse %s: %s — treating as empty", path, e)
        return set(), True
    active: set[str] = set()
    if isinstance(data, list):
        for row in data:
            if not isinstance(row, dict):
                continue
            tid = row.get("task_id")
            status = row.get("status", "ACTIVE")
            if isinstance(tid, str) and tid and status == "ACTIVE":
                active.add(tid)
    elif isinstance(data, dict):
        # Allow a {task_id: row} shape too.
        for tid, row in data.items():
            if not isinstance(row, dict):
                continue
            status = row.get("status", "ACTIVE")
            if isinstance(tid, str) and status == "ACTIVE":
                active.add(tid)
    return active, True


def _run_git(repo: str, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Synchronous git wrapper used by the reconciler.

    Returns ``(returncode, stdout, stderr)``. Never raises; on any
    OS-level failure (timeout, missing git, etc.) the caller sees
    rc != 0 and a non-empty stderr.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _list_fleet_worktrees(repo: str) -> list[tuple[str, str | None]]:
    """Return ``(worktree_path, branch)`` pairs for Fleet-managed worktrees.

    Detection rules:

    * worktree path begins with ``/tmp/fleet-worktrees/``, OR
    * worktree path begins with ``/tmp/fx-``.

    Uses ``git worktree list --porcelain`` for accurate state. Falls
    back to an empty list if git fails (repo missing, etc.).
    """
    rc, out, err = _run_git(repo, ["worktree", "list", "--porcelain"])
    if rc != 0:
        logger.debug("worktree list failed for %s: %s", repo, err.strip())
        return []
    worktrees: list[tuple[str, str | None]] = []
    current_path: str | None = None
    current_branch: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            # End previous record before starting a new one
            if current_path and _is_fleet_managed(current_path):
                worktrees.append((current_path, current_branch))
            current_path = line[len("worktree ") :].strip()
            current_branch = None
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            # ref looks like 'refs/heads/<branch>'
            current_branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line.startswith("detached"):
            current_branch = None
    # Flush trailing record
    if current_path and _is_fleet_managed(current_path):
        worktrees.append((current_path, current_branch))
    return worktrees


def _is_fleet_managed(worktree_path: str) -> bool:
    """Return True iff ``worktree_path`` matches a Fleet pattern."""
    p = worktree_path.rstrip("/")
    if p.startswith(str(_FLEET_WORKTREE_ROOT) + "/"):
        return True
    # Match /tmp/fx-* (legacy pattern).
    return p.startswith("/tmp/" + _FX_WORKTREE_PREFIX)


def _extract_task_id(worktree_path: str) -> str | None:
    """Pull the Fleet task_id out of a worktree path.

    For ``/tmp/fleet-worktrees/<task_id>/...`` → ``<task_id>``.
    For ``/tmp/fx-<suffix>`` → ``fx-<suffix>`` (the whole basename).
    Returns None for unrecognised patterns.
    """
    name = Path(worktree_path.rstrip("/")).name
    parent = Path(worktree_path.rstrip("/")).parent
    if str(parent) == str(_FLEET_WORKTREE_ROOT):
        return name
    if name.startswith(_FX_WORKTREE_PREFIX):
        return name
    return None


def _branch_merged_to_master(repo: str, branch: str) -> tuple[bool, str]:
    """Check whether ``branch`` is fully merged into origin/master.

    Returns ``(is_merged, debug_reason)``. We treat both
    ``origin/master`` (preferred) and the local ``master`` branch as
    the merge target; whichever resolves first wins. If neither
    resolves, we return ``(False, "no master ref")`` so the caller
    can fall back to the age check.
    """
    # Prefer origin/master since A1's lifecycle pushes there before
    # cleanup. Fall back to local master for fully offline repos.
    candidates = ("origin/master", "master", "origin/main", "main")
    base: str | None = None
    for ref in candidates:
        rc, _, _ = _run_git(repo, ["rev-parse", "--verify", ref])
        if rc == 0:
            base = ref
            break
    if not base:
        return False, "no master/main ref"
    # `git merge-base --is-ancestor <branch> <base>` returns 0 when the
    # branch tip is reachable from base — i.e. fully merged.
    rc, _, err = _run_git(repo, ["merge-base", "--is-ancestor", branch, base])
    if rc == 0:
        return True, f"ancestor of {base}"
    return False, f"not ancestor of {base}: {err.strip()[:80]}"


def _branch_last_commit_age_seconds(repo: str, branch: str) -> int | None:
    """Return seconds since ``branch``'s tip commit, or None on failure."""
    rc, out, _ = _run_git(repo, ["log", "-1", "--format=%ct", branch])
    if rc != 0:
        return None
    try:
        ts = int(out.strip())
    except ValueError:
        return None
    now = int(time.time())
    return max(0, now - ts)


# ─── Classification + actions ───────────────────────────────────────────────


def _classify_worktree(
    *,
    repo: str,
    worktree_path: str,
    branch: str | None,
    active_task_ids: set[str],
    stale_threshold_seconds: int,
) -> WorktreeReport:
    """Classify a single Fleet worktree into one of the four verdicts."""
    task_id = _extract_task_id(worktree_path)
    if task_id and task_id in active_task_ids:
        return WorktreeReport(
            repo=repo,
            worktree_path=worktree_path,
            branch=branch,
            verdict=_VERDICT_ACTIVE,
            reason=f"task_id {task_id} is ACTIVE in state file",
        )
    if not branch:
        # Detached HEAD or branchless worktree — we can't safely judge
        # merge state, so fall back to age. If we can't read age either
        # we punt to UNKNOWN.
        return WorktreeReport(
            repo=repo,
            worktree_path=worktree_path,
            branch=None,
            verdict=_VERDICT_UNKNOWN,
            reason="detached HEAD / no branch attached",
        )
    is_merged, merge_reason = _branch_merged_to_master(repo, branch)
    age = _branch_last_commit_age_seconds(repo, branch)
    if is_merged:
        return WorktreeReport(
            repo=repo,
            worktree_path=worktree_path,
            branch=branch,
            verdict=_VERDICT_MERGED,
            reason=merge_reason,
            last_commit_age_seconds=age,
        )
    if age is not None and age >= stale_threshold_seconds:
        return WorktreeReport(
            repo=repo,
            worktree_path=worktree_path,
            branch=branch,
            verdict=_VERDICT_STALE,
            reason=(
                f"last commit is {age // 86400}d old "
                f"(>= {stale_threshold_seconds // 86400}d threshold)"
            ),
            last_commit_age_seconds=age,
        )
    return WorktreeReport(
        repo=repo,
        worktree_path=worktree_path,
        branch=branch,
        verdict=_VERDICT_UNKNOWN,
        reason=(
            f"not merged ({merge_reason}); age {age if age is not None else 'unknown'}s — keeping"
        ),
        last_commit_age_seconds=age,
    )


def _delete_worktree(repo: str, worktree_path: str, branch: str | None) -> str | None:
    """Tear down a worktree + branch. Returns an error string on failure."""
    rc, _, err = _run_git(repo, ["worktree", "remove", "--force", worktree_path])
    # The remove may legitimately fail if the worktree dir is already
    # gone (someone rm'd it manually). Treat absence-of-dir as success.
    if rc != 0 and Path(worktree_path).exists():
        return f"git worktree remove failed: {err.strip()[:200]}"
    if Path(worktree_path).exists():
        try:
            shutil.rmtree(worktree_path, ignore_errors=True)
        except OSError as e:
            return f"shutil.rmtree fallback failed: {e}"
    if branch:
        # branch -D is best-effort; the branch is on origin if A1
        # pushed it, and we don't want to fail the cleanup on a local
        # ref that may already be gone.
        _run_git(repo, ["branch", "-D", branch])
        # Also clean up the remote tracking ref if it's our fleet
        # branch. We don't `git push origin --delete` from the
        # reconciler — A1 owns that. We only clean the LOCAL refs.
    return None


def _apply_verdict(
    worktree_report: WorktreeReport,
    *,
    apply: bool,
    stale_apply: bool,
) -> None:
    """Run the destructive action (or skip) based on the verdict.

    Mutates ``worktree_report.action_taken`` and possibly
    ``worktree_report.error``.
    """
    if worktree_report.verdict == _VERDICT_ACTIVE:
        worktree_report.action_taken = "skipped"
        return
    if worktree_report.verdict == _VERDICT_UNKNOWN:
        worktree_report.action_taken = "skipped"
        return
    # MERGED is auto-cleanable; STALE only on apply+stale_apply.
    is_merged = worktree_report.verdict == _VERDICT_MERGED
    is_stale = worktree_report.verdict == _VERDICT_STALE
    should_delete = (is_merged and apply) or (is_stale and apply and stale_apply)
    if not should_delete:
        worktree_report.action_taken = "none"
        return
    err = _delete_worktree(
        worktree_report.repo,
        worktree_report.worktree_path,
        worktree_report.branch,
    )
    if err:
        worktree_report.action_taken = "error"
        worktree_report.error = err
    else:
        worktree_report.action_taken = "deleted"


# ─── Public API ─────────────────────────────────────────────────────────────


def reconcile(
    *,
    repos: list[str] | None = None,
    state_file: Path | None = None,
    report_path: Path | None = None,
    stale_days: int = _DEFAULT_STALE_DAYS,
    apply: bool = False,
    stale_apply: bool = False,
) -> ReconcileReport:
    """Run the reconciler end-to-end and return the report.

    Parameters
    ----------
    repos
        Repos to scan. Defaults to ``$FLEET_RECONCILER_REPOS`` (colon-
        separated) or the homelab triplet (FX/gitops/dev-infra).
    state_file
        Path to A1's active-dispatches state file.
    report_path
        Where to persist the report. ``None`` writes nowhere.
    stale_days
        Threshold for STALE verdict.
    apply
        If True, MERGED worktrees are deleted. If False (default), the
        whole run is a dry-run.
    stale_apply
        If True AND ``apply`` is True, STALE worktrees are also deleted.
        Default False — operator confirmation is the safer baseline.

    Returns
    -------
    ReconcileReport
        Structured findings; persisted to ``report_path`` when set.
    """
    repos = repos if repos is not None else _resolve_repos_from_env()
    state_file = state_file or _resolve_state_file_from_env()
    report_path = report_path if report_path is not None else _resolve_report_path_from_env()
    stale_seconds = max(1, stale_days) * 86_400

    active, state_present = _load_active_dispatches(state_file)

    report = ReconcileReport(
        generated_at=datetime.now(tz=UTC).isoformat(),
        dry_run=not apply,
        stale_days=stale_days,
        repos_scanned=list(repos),
        state_file=str(state_file),
        state_file_present=state_present,
        active_task_ids=sorted(active),
    )

    for repo in repos:
        if not Path(repo).is_dir():
            logger.info("skipping %s — not a directory", repo)
            continue
        wts = _list_fleet_worktrees(repo)
        for wt_path, wt_branch in wts:
            wr = _classify_worktree(
                repo=repo,
                worktree_path=wt_path,
                branch=wt_branch,
                active_task_ids=active,
                stale_threshold_seconds=stale_seconds,
            )
            _apply_verdict(wr, apply=apply, stale_apply=stale_apply)
            report.worktrees.append(wr)

    # Summary counts
    report.summary = _summarize(report.worktrees)

    # Side effect: emit prometheus orphan count (best-effort).
    _record_metrics(report)

    if report_path is not None:
        try:
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        except OSError as e:
            logger.warning("could not write report to %s: %s", report_path, e)

    return report


def _summarize(rows: list[WorktreeReport]) -> dict[str, int]:
    """Count rows by verdict and by action."""
    summary: dict[str, int] = {
        "total": len(rows),
        "active": 0,
        "merged": 0,
        "stale": 0,
        "unknown": 0,
        "deleted": 0,
        "errors": 0,
    }
    for r in rows:
        summary[r.verdict] = summary.get(r.verdict, 0) + 1
        if r.action_taken == "deleted":
            summary["deleted"] += 1
        elif r.action_taken == "error":
            summary["errors"] += 1
    return summary


def _record_metrics(report: ReconcileReport) -> None:
    """Push the orphan + worktree counts to the Prometheus facade.

    Best-effort: import lazily so the reconciler is usable in
    environments without the metrics module wired.
    """
    try:
        from . import metrics

        facade = metrics.get()
        # Sum stale+unknown as "orphaned" (they're branches Fleet didn't
        # clean up itself); active+merged are healthy. We use the
        # delta-from-previous-call pattern via inc(), so the metric is
        # cumulative across reconciler runs — Prometheus rate() then
        # gives you orphans/day.
        orphans = report.summary.get("stale", 0) + report.summary.get("unknown", 0)
        if orphans:
            facade.branch_orphaned(count=orphans)
        facade.set_worktrees_active(report.summary.get("total", 0))
    except Exception as e:
        logger.debug("metrics emit skipped: %s", e)


# ─── Env-var resolution ─────────────────────────────────────────────────────


def _resolve_repos_from_env() -> list[str]:
    raw = os.environ.get("FLEET_RECONCILER_REPOS", "").strip()
    if not raw:
        return list(_DEFAULT_REPOS)
    return [p.strip() for p in raw.split(":") if p.strip()]


def _resolve_state_file_from_env() -> Path:
    raw = os.environ.get("FLEET_RECONCILER_STATE_FILE", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_STATE_FILE


def _resolve_report_path_from_env() -> Path:
    raw = os.environ.get("FLEET_RECONCILER_REPORT_PATH", "").strip()
    return Path(raw).expanduser() if raw else _DEFAULT_REPORT_PATH


# ─── CLI entrypoint ─────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleet-reconciler",
        description=(
            "Detect + clean up orphaned Fleet-managed git worktrees + "
            "branches. Defaults to dry-run; pass --apply to actually "
            "delete merged worktrees, and --apply --stale-apply to "
            "additionally delete worktrees older than the stale "
            "threshold."
        ),
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually delete merged worktrees (default: dry-run)",
    )
    p.add_argument(
        "--stale-apply",
        action="store_true",
        help="also delete STALE worktrees (requires --apply)",
    )
    p.add_argument(
        "--repo",
        action="append",
        default=None,
        help=(
            "repo path to scan (repeat for multiple). Overrides "
            "FLEET_RECONCILER_REPOS and the homelab defaults."
        ),
    )
    p.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="path to A1's active_dispatches.json",
    )
    p.add_argument(
        "--report-path",
        type=Path,
        default=None,
        help="where to write the JSON report",
    )
    p.add_argument(
        "--stale-days",
        type=int,
        default=_DEFAULT_STALE_DAYS,
        help=f"stale threshold in days (default: {_DEFAULT_STALE_DAYS})",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="emit the JSON report to stdout in addition to the file",
    )
    p.add_argument(
        "--quiet",
        action="store_true",
        help="suppress the human-readable summary",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    ns = parser.parse_args(argv)
    logging.basicConfig(
        level=os.environ.get("FLEET_RECONCILER_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    repos = ns.repo if ns.repo else None
    report = reconcile(
        repos=repos,
        state_file=ns.state_file,
        report_path=ns.report_path,
        stale_days=ns.stale_days,
        apply=ns.apply,
        stale_apply=ns.stale_apply,
    )
    if not ns.quiet:
        _print_summary(report)
    if ns.json:
        print(json.dumps(report.to_dict(), indent=2))
    # Exit code: 0 when nothing actionable; 1 when we deleted things or
    # when errors were recorded; 2 when STALE/UNKNOWN need operator
    # attention.
    if report.summary.get("errors", 0):
        return 1
    if report.summary.get("stale", 0) or report.summary.get("unknown", 0):
        return 2 if report.dry_run else 0
    return 0


def _print_summary(report: ReconcileReport) -> None:
    print(f"# fleet-reconciler ({'DRY-RUN' if report.dry_run else 'APPLY'})")
    print(f"  generated_at:    {report.generated_at}")
    print(f"  repos_scanned:   {', '.join(report.repos_scanned)}")
    print(f"  state_file:      {report.state_file} (present={report.state_file_present})")
    print(f"  active_task_ids: {len(report.active_task_ids)}")
    print(f"  worktrees:       {report.summary.get('total', 0)}")
    print(
        f"    active={report.summary.get('active', 0)} "
        f"merged={report.summary.get('merged', 0)} "
        f"stale={report.summary.get('stale', 0)} "
        f"unknown={report.summary.get('unknown', 0)}"
    )
    print(
        f"    deleted={report.summary.get('deleted', 0)} errors={report.summary.get('errors', 0)}"
    )
    for wt in report.worktrees:
        marker = {
            "deleted": "  DEL",
            "skipped": "  SKP",
            "none": "    .",
            "error": "  ERR",
        }.get(wt.action_taken, "    ?")
        print(f"{marker} [{wt.verdict:8s}] {wt.worktree_path} ({wt.branch or '-'})")
        if wt.reason:
            print(f"          reason: {wt.reason}")
        if wt.error:
            print(f"          error:  {wt.error}")


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
