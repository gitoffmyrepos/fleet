"""Shared subprocess-runner with circuit guard + telemetry."""

from __future__ import annotations

import abc
import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..circuit import CircuitOpen, CircuitRegistry
from ..telemetry import Telemetry

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    ok: bool
    task_id: str
    summary: dict[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    exit_code: int | None = None
    duration_seconds: float = 0.0
    # Persistence — populated by _commit_and_push when applicable.
    commit_sha: str | None = None
    pushed: bool = False
    persistence_note: str = ""
    # Isolation — populated when isolation="worktree" was used.
    worktree_path: str | None = None
    worktree_branch: str | None = None


class DispatcherBase(abc.ABC):
    upstream_name: ClassVar[str] = "unknown"

    def __init__(
        self,
        *,
        circuits: CircuitRegistry,
        telemetry: Telemetry,
        timeout_seconds: int,
    ) -> None:
        self._reg = circuits
        self._t = telemetry
        self._timeout = timeout_seconds

    @abc.abstractmethod
    def cli_args(self, **kwargs: Any) -> list[str]: ...

    def env(self, **kwargs: Any) -> dict[str, str] | None:
        """Return the environment for the subprocess, or None to inherit.

        WARNING: returning a dict *replaces* the entire process environment.
        Subclasses that want to ADD variables must merge with os.environ:

            import os
            def env(self, **kwargs: Any) -> dict[str, str] | None:
                return {**os.environ, "MY_VAR": "value"}

        Returning None (the default) inherits the parent process env, which
        is what most subclasses want.
        """
        return None

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        return {"stdout_tail": stdout[-500:]}

    async def dispatch(self, *, task_id: str, **kwargs: Any) -> DispatchResult:
        t0 = time.monotonic()
        breaker = self._reg.get(self.upstream_name)
        try:
            breaker.guard()
        except CircuitOpen as e:
            await self._t.failure(
                task_id=task_id,
                reason="circuit_open",
                body={
                    "upstream": self.upstream_name,
                    "retry_after_seconds": e.retry_after_seconds,
                },
            )
            return DispatchResult(
                ok=False,
                task_id=task_id,
                error=f"circuit '{self.upstream_name}' open; retry in {e.retry_after_seconds:.0f}s",
                duration_seconds=time.monotonic() - t0,
            )

        # cwd: where the spawned agent should write. Defaults to the daemon's
        # CWD, which historically caused agents to write to /home/.../fleet/
        # instead of the task's project. Callers SHOULD pass cwd explicitly.
        cwd: str | None = kwargs.get("cwd")
        if cwd is not None and not Path(cwd).is_dir():
            return DispatchResult(
                ok=False,
                task_id=task_id,
                error=f"cwd does not exist or is not a directory: {cwd}",
                duration_seconds=time.monotonic() - t0,
            )

        # Isolation: when isolation="worktree" and cwd is a git repo, create
        # an isolated git worktree so parallel dispatches against the same
        # cwd don't cross-contaminate via `git add -A` at commit time.
        # (2026-05-10 incident: two parallel subagents → first to finish
        # swept the other's in-flight files into its own commit.)
        isolation: str | None = kwargs.get("isolation")
        worktree_path: str | None = None
        worktree_branch: str | None = None
        original_cwd = cwd
        if isolation == "worktree" and cwd:
            wt_path, wt_branch, wt_err = await self._make_worktree(source_repo=cwd, task_id=task_id)
            if wt_err:
                # Worktree creation failed — fall back to shared cwd with a
                # warning rather than hard-fail. Caller can still proceed.
                logger.warning(
                    "worktree isolation failed for %s, falling back to shared cwd: %s",
                    task_id,
                    wt_err,
                )
            else:
                cwd = wt_path
                worktree_path = wt_path
                worktree_branch = wt_branch

        # If we redirected cwd to a worktree, propagate to BOTH `cwd` and
        # `workdir` kwargs (the swarm dispatcher uses `workdir` for
        # --workdir + PWD; subagent + verify + phase use `cwd`).
        run_kwargs = {**kwargs, "cwd": cwd}
        if worktree_path:
            run_kwargs["workdir"] = cwd
        args = self.cli_args(**run_kwargs)
        await self._t.start(
            task_id=task_id,
            kind=self.upstream_name,
            body={
                "args": args[:6],
                "cwd": cwd or os.getcwd(),
                "isolation": isolation,
                "worktree_branch": worktree_branch,
            },
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env(**run_kwargs),
            cwd=cwd,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except TimeoutError:
                proc.kill()
                await proc.wait()
            finally:
                # Drain pipe handles so transports release promptly.
                if proc.stdout:
                    proc.stdout.feed_eof()
                if proc.stderr:
                    proc.stderr.feed_eof()
            breaker.record_failure()
            await self._t.failure(
                task_id=task_id,
                reason="timeout",
                body={"timeout_seconds": self._timeout},
            )
            return DispatchResult(
                ok=False,
                task_id=task_id,
                error="timeout",
                exit_code=None,
                duration_seconds=time.monotonic() - t0,
            )

        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            breaker.record_failure()
            err = f"exit code {proc.returncode}: {stderr[-200:]}"
            await self._t.failure(
                task_id=task_id,
                reason=f"exit_code_{proc.returncode}",
                body={"exit_code": proc.returncode, "stderr_tail": stderr[-200:]},
            )
            # Tear down worktree on failure (no commits, no merge).
            if worktree_path:
                await self._remove_worktree(
                    source_repo=original_cwd or worktree_path,
                    worktree_path=worktree_path,
                    branch=worktree_branch,
                )
            return DispatchResult(
                ok=False,
                task_id=task_id,
                stdout=stdout,
                stderr=stderr,
                error=err,
                exit_code=proc.returncode,
                duration_seconds=time.monotonic() - t0,
                worktree_path=None,
                worktree_branch=None,
            )

        breaker.record_success()
        summary = self.parse_summary(stdout, stderr, **kwargs)

        # Commit + push the agent's work so it persists. Skipped when:
        #   - cwd not passed (agent ran in daemon CWD; nothing safe to commit)
        #   - cwd is not a git repo
        #   - no working-tree changes since dispatch started
        #   - caller passed auto_commit=False
        commit_sha: str | None = None
        pushed = False
        persistence_note = ""
        if cwd and bool(kwargs.get("auto_commit", True)):
            commit_sha, pushed, persistence_note = await self._commit_and_push(
                cwd=cwd,
                task_id=task_id,
                task=str(kwargs.get("task", "")),
                worktree_branch=worktree_branch,
                source_repo=original_cwd if worktree_path else None,
            )

        # If we used a worktree, clean it up after commit/push regardless of
        # outcome — the branch (if any) is already on origin or in the source
        # repo's refs. The worktree dir itself is ephemeral.
        wt_path_for_result = worktree_path
        wt_branch_for_result = worktree_branch
        if worktree_path:
            # Only keep the worktree path/branch in the result if no commit
            # landed — that means the caller may want to inspect manually.
            kept = commit_sha is None and persistence_note != "no changes to commit"
            if not kept:
                await self._remove_worktree(
                    source_repo=original_cwd or worktree_path,
                    worktree_path=worktree_path,
                    branch=worktree_branch,
                )
                wt_path_for_result = None
                wt_branch_for_result = None

        await self._t.end(
            task_id=task_id,
            ok=True,
            body={
                "summary": summary,
                "exit_code": 0,
                "commit_sha": commit_sha,
                "pushed": pushed,
                "persistence_note": persistence_note,
                "worktree_path": wt_path_for_result,
                "worktree_branch": wt_branch_for_result,
            },
        )
        return DispatchResult(
            ok=True,
            task_id=task_id,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            exit_code=0,
            duration_seconds=time.monotonic() - t0,
            commit_sha=commit_sha,
            pushed=pushed,
            persistence_note=persistence_note,
            worktree_path=wt_path_for_result,
            worktree_branch=wt_branch_for_result,
        )

    async def _commit_and_push(
        self,
        *,
        cwd: str,
        task_id: str,
        task: str,
        worktree_branch: str | None = None,
        source_repo: str | None = None,
    ) -> tuple[str | None, bool, str]:
        """Commit working-tree changes + push.

        Returns (commit_sha, pushed, note). Never raises — persistence is
        best-effort; the agent's work already happened on disk.

        When worktree_branch is set (isolation="worktree" path), commits on
        the worktree's branch and pushes it to origin as a feature branch
        AND fast-forwards origin/master if origin/master == merge-base.
        That gives us isolation without losing the "lands on master" behavior.

        Skips cleanly when cwd isn't a git repo or nothing changed.
        """
        # 1. Is cwd a git repo?
        try:
            rc, top, _ = await self._git(cwd, ["rev-parse", "--show-toplevel"])
        except FileNotFoundError:
            return None, False, "git binary not found"
        if rc != 0:
            return None, False, "cwd is not a git repo (skipped commit/push)"
        repo_top = top.strip()

        # 2. Anything to commit?
        rc, status, _ = await self._git(repo_top, ["status", "--porcelain"])
        if rc != 0:
            return None, False, f"git status failed: rc={rc}"
        if not status.strip():
            return None, False, "no changes to commit"

        # 3. Stage everything (mirrors the user's "commit and push code when done").
        #    In worktree mode, git's auto-scoping means `git add -A` here only
        #    touches the isolated working tree — no cross-contamination with
        #    parallel dispatches operating on the source repo or other
        #    worktrees.
        rc, _, err = await self._git(repo_top, ["add", "-A"])
        if rc != 0:
            return None, False, f"git add failed: {err.strip()[:200]}"

        # 4. Commit. Subject: short task tag; body: full task + task_id.
        short_task = task.replace("\n", " ").strip()[:72] or "fleet dispatch"
        msg = f"fleet({self.upstream_name}): {short_task}\n\nTask-Id: {task_id}\n"
        rc, out, err = await self._git(repo_top, ["commit", "-m", msg])
        if rc != 0:
            # `nothing to commit` race (someone else committed concurrently)
            return None, False, f"git commit failed: {err.strip()[:200] or out.strip()[:200]}"
        rc, sha, _ = await self._git(repo_top, ["rev-parse", "HEAD"])
        commit_sha = sha.strip() if rc == 0 else None

        # 5. Push.
        if worktree_branch:
            # Worktree path: push the feature branch + try to fast-forward
            # origin/master so the work lands without manual PR step.
            rc, _, err = await self._git(
                repo_top, ["push", "origin", f"HEAD:refs/heads/{worktree_branch}"]
            )
            if rc != 0:
                return (
                    commit_sha,
                    False,
                    (
                        f"committed locally on {worktree_branch}; push branch failed: "
                        f"{err.strip()[:200]}"
                    ),
                )
            # Fetch master ref to see if our commit fast-forwards from it.
            await self._git(repo_top, ["fetch", "origin", "master"])
            rc, base_sha, _ = await self._git(repo_top, ["merge-base", "HEAD", "origin/master"])
            rc2, master_sha, _ = await self._git(repo_top, ["rev-parse", "origin/master"])
            ff_eligible = rc == 0 and rc2 == 0 and base_sha.strip() == master_sha.strip()
            if ff_eligible:
                rc, _, err = await self._git(repo_top, ["push", "origin", "HEAD:refs/heads/master"])
                if rc == 0:
                    return (
                        commit_sha,
                        True,
                        (
                            f"committed on worktree branch {worktree_branch}; "
                            f"fast-forwarded origin/master"
                        ),
                    )
                # FF push failed (someone else pushed concurrently). The
                # feature branch is still on origin — operator can merge it.
                return (
                    commit_sha,
                    True,
                    (
                        f"committed on worktree branch {worktree_branch}; "
                        f"master FF push lost a race ({err.strip()[:120]}) — "
                        f"merge {worktree_branch} manually."
                    ),
                )
            return (
                commit_sha,
                True,
                (
                    f"committed on worktree branch {worktree_branch}; "
                    f"diverged from origin/master, not auto-merging."
                ),
            )

        # Non-worktree path (back-compat): push current branch to origin.
        rc, _, err = await self._git(repo_top, ["push", "origin", "HEAD"])
        if rc != 0:
            return commit_sha, False, f"committed locally but push failed: {err.strip()[:200]}"

        return commit_sha, True, "committed and pushed"

    # --------- worktree isolation helpers ----------

    async def _make_worktree(
        self, *, source_repo: str, task_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Create an isolated git worktree off source_repo's HEAD.

        Returns (worktree_path, branch_name, error). On failure all three
        are (None, None, error_message) — callers should fall back to
        shared cwd with a warning.

        Worktrees live under /tmp/fleet-worktrees/<task_id>/ to keep the
        source repo's tree free of fleet bookkeeping dirs (which would
        otherwise show up in `git status` and confuse human operators).
        """
        # Verify source is a git repo
        rc, top, _ = await self._git(source_repo, ["rev-parse", "--show-toplevel"])
        if rc != 0:
            return None, None, "source is not a git repo"
        repo_top = top.strip()

        # Worktree path + branch name
        wt_root = Path("/tmp/fleet-worktrees")
        wt_root.mkdir(parents=True, exist_ok=True)
        wt_path = str(wt_root / task_id)
        if Path(wt_path).exists():
            # Stale worktree from a crashed previous run — try to remove
            # cleanly via git so refs/worktrees gets cleaned up too.
            await self._git(repo_top, ["worktree", "remove", "--force", wt_path])
            # If still there (race / external removal), nuke it.
            if Path(wt_path).exists():
                import shutil

                shutil.rmtree(wt_path, ignore_errors=True)
        branch = f"fleet/{task_id}"

        # Create branch + worktree in one shot
        rc, _, err = await self._git(repo_top, ["worktree", "add", "-b", branch, wt_path, "HEAD"])
        if rc != 0:
            # Branch may already exist (re-dispatch of same task_id) — try
            # without -b
            rc2, _, err2 = await self._git(repo_top, ["worktree", "add", wt_path, branch])
            if rc2 != 0:
                return (
                    None,
                    None,
                    (f"git worktree add failed: {err.strip()[:200]} / " f"{err2.strip()[:200]}"),
                )
        return wt_path, branch, None

    async def _remove_worktree(
        self,
        *,
        source_repo: str,
        worktree_path: str,
        branch: str | None,
    ) -> None:
        """Tear down a worktree + its branch. Best-effort, never raises."""
        try:
            rc, top, _ = await self._git(source_repo, ["rev-parse", "--show-toplevel"])
            repo_top = top.strip() if rc == 0 else source_repo
            # remove the worktree (force in case the agent left dirty files)
            await self._git(repo_top, ["worktree", "remove", "--force", worktree_path])
            if branch:
                # delete the local branch — origin still has it if pushed
                await self._git(repo_top, ["branch", "-D", branch])
            # Belt-and-suspenders for case where worktree remove silently failed
            if Path(worktree_path).exists():
                import shutil

                shutil.rmtree(worktree_path, ignore_errors=True)
        except Exception as e:
            logger.debug("worktree teardown error (non-fatal): %s", e)

    @staticmethod
    async def _git(cwd: str, argv: list[str]) -> tuple[int, str, str]:
        """Run a git subcommand under cwd. Returns (rc, stdout, stderr)."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except FileNotFoundError as e:
            logger.debug("git binary missing: %s", e)
            raise
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=60)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, "", "timeout"
        return (
            proc.returncode if proc.returncode is not None else -1,
            out_b.decode("utf-8", "replace"),
            err_b.decode("utf-8", "replace"),
        )
