"""Active-dispatch state tracking for crash recovery.

Each active dispatch (worktree + branch + cwd) is recorded in a JSON state
file under ``~/.local/state/fleet/active_dispatches.json``. The dispatcher
appends an entry when it creates a worktree and removes it when the
worktree is torn down (success or explicit cleanup). If the orchestrator
crashes mid-dispatch the entry stays on disk so a restart can reconcile
abandoned worktrees + branches.

The state file is a single JSON object keyed by task_id so writes are
idempotent across reorderings. Atomic writes use a temp-file + rename.

This module is intentionally self-contained: no asyncio, no third-party
deps, no telemetry. It must work when the rest of Fleet is broken.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_DIR = Path.home() / ".local" / "state" / "fleet"
_STATE_PATH = _STATE_DIR / "active_dispatches.json"


@dataclass(frozen=True)
class ActiveDispatch:
    """Snapshot of a live dispatch — written when worktree is created."""

    task_id: str
    upstream_name: str
    source_repo: str
    worktree_path: str
    worktree_branch: str
    started_at: float
    # Optional extra context for operator forensics.
    extra: dict[str, Any] = field(default_factory=dict)


def _read_state(path: Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the state file; missing/corrupt → empty dict."""
    if path is None:
        path = _STATE_PATH
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw) if raw.strip() else {}
        if not isinstance(data, dict):
            logger.warning("state file %s has unexpected shape; ignoring", path)
            return {}
        return data
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read state file %s: %s", path, exc)
        return {}


def _atomic_write(payload: dict[str, dict[str, Any]], path: Path | None = None) -> None:
    """Atomically replace ``path`` with ``payload`` (JSON)."""
    if path is None:
        path = _STATE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    # NamedTemporaryFile in the same directory so the rename is atomic on POSIX.
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".tmp-", dir=str(path.parent), suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except OSError as exc:
        logger.warning("could not write state file %s: %s", path, exc)
        # best-effort cleanup of orphan tmp on failure
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)


def record_dispatch(entry: ActiveDispatch, path: Path | None = None) -> None:
    """Add or replace an entry keyed by task_id. Best-effort, never raises."""
    if path is None:
        path = _STATE_PATH
    try:
        state = _read_state(path)
        state[entry.task_id] = asdict(entry)
        _atomic_write(state, path)
    except Exception as exc:  # paranoid: state tracking must NEVER kill dispatch
        logger.warning("record_dispatch(%s) failed (non-fatal): %s", entry.task_id, exc)


def remove_dispatch(task_id: str, path: Path | None = None) -> None:
    """Remove the entry for ``task_id``. Idempotent; never raises."""
    if path is None:
        path = _STATE_PATH
    try:
        state = _read_state(path)
        if task_id in state:
            state.pop(task_id, None)
            _atomic_write(state, path)
    except Exception as exc:
        logger.warning("remove_dispatch(%s) failed (non-fatal): %s", task_id, exc)


def list_active(path: Path | None = None) -> list[ActiveDispatch]:
    """Return all active dispatches recorded on disk."""
    if path is None:
        path = _STATE_PATH
    state = _read_state(path)
    out: list[ActiveDispatch] = []
    for tid, body in state.items():
        try:
            out.append(
                ActiveDispatch(
                    task_id=body.get("task_id", tid),
                    upstream_name=body.get("upstream_name", "unknown"),
                    source_repo=body["source_repo"],
                    worktree_path=body["worktree_path"],
                    worktree_branch=body["worktree_branch"],
                    started_at=float(body.get("started_at", 0.0)),
                    extra=body.get("extra") or {},
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("skipping malformed state entry %s: %s", tid, exc)
    return out


def state_path() -> Path:
    """Return the state file path (override target for tests)."""
    return _STATE_PATH


def reconcile_active_dispatches(
    path: Path | None = None,
    *,
    age_threshold_seconds: float = 0.0,
) -> list[dict[str, Any]]:
    """Reconcile dispatch state vs. on-disk worktrees.

    Walk the state file and return a list of orphan entries — those whose
    worktree directory still exists on disk AND whose ``started_at`` is
    older than ``age_threshold_seconds`` (so live dispatches in another
    process don't get clobbered).

    The orchestrator calls this on startup and logs the result. It does
    NOT auto-remove orphans — that's a human decision, especially for
    diverged branches where the operator may still want to inspect.

    Returns a list of dicts: ``{task_id, worktree_path, worktree_branch,
    age_seconds, exists_on_disk, source_repo}``.
    """
    import time

    out: list[dict[str, Any]] = []
    now = time.time()
    for entry in list_active(path):
        age = now - entry.started_at
        if age < age_threshold_seconds:
            continue
        exists = Path(entry.worktree_path).exists()
        out.append(
            {
                "task_id": entry.task_id,
                "upstream_name": entry.upstream_name,
                "source_repo": entry.source_repo,
                "worktree_path": entry.worktree_path,
                "worktree_branch": entry.worktree_branch,
                "age_seconds": age,
                "exists_on_disk": exists,
                "started_at": entry.started_at,
            }
        )
    return out
