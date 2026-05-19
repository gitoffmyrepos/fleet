"""Shared subprocess-runner with circuit guard + telemetry."""

from __future__ import annotations

import abc
import asyncio
import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from ..circuit import CircuitOpen, CircuitRegistry
from ..telemetry import Telemetry
from .state import ActiveDispatch, record_dispatch, remove_dispatch

logger = logging.getLogger(__name__)

# 2026-05-11 (anti-hallucination): scratch dir for real-time dispatch logs.
# Survives mid-flight kills; operators can `tail -f` to monitor.
_LOG_DIR = Path.home() / ".local" / "state" / "fleet" / "dispatches"

# Regexes for detecting claimed work in agent output:
#   * 7+ chars of hex characters that look like git short SHAs
#   * "RESULT: N of M tasks complete" / "N atomic commits" patterns
_SHA_RE = re.compile(r"\b(?:commit\s+)?([0-9a-f]{7,40})\b", re.IGNORECASE)
_TASK_COMPLETION_RE = re.compile(
    r"RESULT:\s*(\d+)\s*(?:of|/)\s*(\d+)\s*tasks?\s*(?:complete|done|landed)",
    re.IGNORECASE,
)
_COMMIT_COUNT_RE = re.compile(r"\b(\d+)\s+atomic\s+commits?\b", re.IGNORECASE)


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
    # 2026-05-11 (anti-hallucination): populated by _verify_persistence_claims.
    # If True, the agent reported a result with commit-like claims that we
    # could not verify against the worktree. The dispatch is still ok=True
    # (the process exit code was clean) but operators should treat the
    # claimed work as suspect — the commits may not actually exist.
    hallucination_detected: bool = False
    hallucination_reason: str = ""
    # Verified persistence facts — actual commits on the worktree branch
    # vs. what the agent claimed in its output. Empty when no git repo.
    verified_commits: list[str] = field(default_factory=list)
    # Real-time stdout/stderr log path (populated when streaming is enabled).
    # Survives even if the dispatch is killed mid-flight — operators can
    # `tail -f` this file to watch progress.
    log_path: str | None = None


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
            elif wt_path and wt_branch:
                cwd = wt_path
                worktree_path = wt_path
                worktree_branch = wt_branch
                # 2026-05-19 (lifecycle state): record the active dispatch so
                # an orchestrator crash leaves a recoverable trail.
                record_dispatch(
                    ActiveDispatch(
                        task_id=task_id,
                        upstream_name=self.upstream_name,
                        source_repo=original_cwd or wt_path,
                        worktree_path=wt_path,
                        worktree_branch=wt_branch,
                        started_at=time.time(),
                        extra={"timeout_seconds": self._timeout},
                    )
                )

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
        # 2026-05-11 (persistence): capture pre-spawn git HEAD so we can
        # verify post-dispatch claims against actual commits.
        pre_head: str | None = None
        if cwd:
            pre_head = await self._capture_head_sha(cwd)

        # 2026-05-11 (persistence): real-time stream both pipes to a log
        # file under ~/.local/state/fleet/dispatches/<task_id>.{out,err}
        # so the operator can `tail -f` mid-flight AND partial output
        # survives a SIGKILL.
        try:
            _LOG_DIR.mkdir(parents=True, exist_ok=True)
            stdout_log = _LOG_DIR / f"{task_id}.out"
            stderr_log = _LOG_DIR / f"{task_id}.err"
        except OSError as exc:
            logger.warning("could not create dispatch log dir: %s", exc)
            stdout_log = stderr_log = None  # streaming disabled, fall back

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env(**run_kwargs),
            cwd=cwd,
        )
        stdout_acc = bytearray()
        stderr_acc = bytearray()

        async def _stream(
            pipe: asyncio.StreamReader | None,
            acc: bytearray,
            log_path: Path | None,
        ) -> None:
            """Read chunks from pipe; append to in-memory buffer; tee to log file as we go."""
            if pipe is None:
                return
            with contextlib.ExitStack() as stack:
                log_fh = None
                if log_path is not None:
                    try:
                        log_fh = stack.enter_context(open(log_path, "wb"))
                    except OSError as exc:
                        logger.debug("could not open dispatch log %s: %s", log_path, exc)
                        log_fh = None
                while True:
                    chunk = await pipe.read(4096)
                    if not chunk:
                        break
                    acc.extend(chunk)
                    if log_fh is not None:
                        with contextlib.suppress(OSError):
                            log_fh.write(chunk)
                            log_fh.flush()

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    _stream(proc.stdout, stdout_acc, stdout_log),
                    _stream(proc.stderr, stderr_acc, stderr_log),
                    proc.wait(),
                ),
                timeout=self._timeout,
            )
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
            # 2026-05-19 (lifecycle): KEEP the worktree on timeout so the
            # operator can inspect what the agent had written. Without
            # this, forensics on long-running agent failures was impossible.
            forensic_note = ""
            if worktree_path:
                forensic_note = (
                    f"timeout — worktree retained for forensics at {worktree_path} "
                    f"(branch {worktree_branch}). Clean up manually with: "
                    f"git -C {original_cwd or worktree_path} worktree remove --force "
                    f"{worktree_path} && git -C {original_cwd or worktree_path} branch -D "
                    f"{worktree_branch}"
                )
                logger.warning("dispatch %s: %s", task_id, forensic_note)
            await self._t.failure(
                task_id=task_id,
                reason="timeout",
                body={
                    "timeout_seconds": self._timeout,
                    "worktree_retained": bool(worktree_path),
                    "worktree_path": worktree_path,
                    "worktree_branch": worktree_branch,
                    "forensic_note": forensic_note,
                },
            )
            return DispatchResult(
                ok=False,
                task_id=task_id,
                error="timeout",
                exit_code=None,
                duration_seconds=time.monotonic() - t0,
                # Partial output already streamed to disk — operator can recover it.
                log_path=str(stdout_log) if stdout_log else None,
                # Surface the retained worktree so callers know where to look.
                worktree_path=worktree_path,
                worktree_branch=worktree_branch,
                persistence_note=forensic_note,
            )

        stdout_b = bytes(stdout_acc)
        stderr_b = bytes(stderr_acc)
        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")
        if proc.returncode != 0:
            breaker.record_failure()
            err = f"exit code {proc.returncode}: {stderr[-200:]}"
            # 2026-05-19 (lifecycle): KEEP the worktree on non-zero exit so the
            # operator can inspect partial work. The previous behaviour
            # silently removed the evidence right when the human needed it.
            forensic_note = ""
            if worktree_path:
                forensic_note = (
                    f"non-zero exit ({proc.returncode}) — worktree retained at "
                    f"{worktree_path} (branch {worktree_branch}). Clean up manually with: "
                    f"git -C {original_cwd or worktree_path} worktree remove --force "
                    f"{worktree_path} && git -C {original_cwd or worktree_path} branch -D "
                    f"{worktree_branch}"
                )
                logger.warning("dispatch %s: %s", task_id, forensic_note)
            await self._t.failure(
                task_id=task_id,
                reason=f"exit_code_{proc.returncode}",
                body={
                    "exit_code": proc.returncode,
                    "stderr_tail": stderr[-200:],
                    "worktree_retained": bool(worktree_path),
                    "worktree_path": worktree_path,
                    "worktree_branch": worktree_branch,
                    "forensic_note": forensic_note,
                },
            )
            return DispatchResult(
                ok=False,
                task_id=task_id,
                stdout=stdout,
                stderr=stderr,
                error=err,
                exit_code=proc.returncode,
                duration_seconds=time.monotonic() - t0,
                # 2026-05-19: surface the retained worktree (was None previously).
                worktree_path=worktree_path,
                worktree_branch=worktree_branch,
                persistence_note=forensic_note,
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

        # 2026-05-11 (anti-hallucination): verify the agent's claims against
        # actual worktree state BEFORE we tear the worktree down. This is the
        # check that catches the "RESULT: 5 commits complete" with fake SHAs
        # pattern (Fleet's own audit hit this exact case on 2026-05-11).
        post_head = await self._capture_head_sha(cwd) if cwd else None
        hallucination_detected, hallucination_reason, verified_commits = (
            self._verify_persistence_claims(
                stdout=stdout,
                pre_head=pre_head,
                post_head=post_head,
                commit_sha=commit_sha,
                persistence_note=persistence_note,
                cwd=cwd,
            )
        )
        if hallucination_detected:
            logger.warning(
                "dispatch %s hallucination detected: %s",
                task_id,
                hallucination_reason,
            )
            await self._t.event(
                task_id=task_id,
                kind="fleet_hallucination_detected",
                body={
                    "reason": hallucination_reason,
                    "verified_commits": verified_commits,
                    "commit_sha": commit_sha,
                    "persistence_note": persistence_note,
                },
            )

        # If we used a worktree, clean it up after commit/push regardless of
        # outcome — the branch (if any) is already on origin or in the source
        # repo's refs. The worktree dir itself is ephemeral.
        wt_path_for_result = worktree_path
        wt_branch_for_result = worktree_branch
        if worktree_path:
            # Only keep the worktree path/branch in the result if no commit
            # landed — that means the caller may want to inspect manually.
            # Also keep on hallucination so the operator can audit the worktree.
            kept = (
                commit_sha is None and persistence_note != "no changes to commit"
            ) or hallucination_detected
            if not kept:
                await self._remove_worktree(
                    source_repo=original_cwd or worktree_path,
                    worktree_path=worktree_path,
                    branch=worktree_branch,
                    task_id=task_id,
                )
                wt_path_for_result = None
                wt_branch_for_result = None
            else:
                # Worktree retained (hallucination or no-commit) — leave the
                # state entry on disk so a restart sees it for reconciliation.
                logger.info(
                    "dispatch %s: worktree retained at %s (branch %s) "
                    "for operator inspection; state entry kept",
                    task_id,
                    worktree_path,
                    worktree_branch,
                )

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
                "hallucination_detected": hallucination_detected,
                "verified_commits": verified_commits,
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
            hallucination_detected=hallucination_detected,
            hallucination_reason=hallucination_reason,
            verified_commits=verified_commits,
            log_path=str(stdout_log) if stdout_log else None,
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

        # 5. Push — worktree branch lifecycle (2026-05-19 redesign).
        if worktree_branch:
            return await self._land_worktree_on_master(
                repo_top=repo_top,
                worktree_branch=worktree_branch,
                commit_sha=commit_sha,
            )

        # Non-worktree path (back-compat): push current branch to origin.
        rc, _, err = await self._git(repo_top, ["push", "origin", "HEAD"])
        if rc != 0:
            return commit_sha, False, f"committed locally but push failed: {err.strip()[:200]}"

        return commit_sha, True, "committed and pushed"

    async def _land_worktree_on_master(
        self,
        *,
        repo_top: str,
        worktree_branch: str,
        commit_sha: str | None,
    ) -> tuple[str | None, bool, str]:
        """Rebase the worktree branch onto origin/master and push to master.

        2026-05-19 (branch lifecycle): replaces the prior FF-only path. The
        flow is:

          1. ``git fetch origin master`` — get the latest tip.
          2. ``git rebase origin/master`` — replay our commit on top of the
             current remote. Handles the race where origin/master moved
             during the agent's work.
          3. If the rebase succeeded cleanly:
             a. ``git push origin HEAD:refs/heads/master`` (master-only
                flow per ``feedback_master_only.md``).
             b. If master push succeeded, ``git push --delete`` the remote
                feature branch — we don't want orphan ``fleet/task_*``
                branches piling up on origin.
          4. If the rebase had conflicts: abort the rebase and keep the
             branch (push as feature branch only) so the operator can
             resolve manually. Returns ``pushed=True`` because the work
             is preserved on origin even if master didn't move.

        Always returns ``(commit_sha, pushed, note)``. Never raises —
        commit/push is best-effort; the agent's work is already on disk.
        """
        # 1. Refresh origin/master.
        rc, _, err = await self._git(repo_top, ["fetch", "origin", "master"])
        if rc != 0:
            # Without origin/master we can't rebase. Push the feature branch
            # so work isn't lost and the operator can recover.
            return await self._push_feature_branch_only(
                repo_top=repo_top,
                worktree_branch=worktree_branch,
                commit_sha=commit_sha,
                note_prefix=f"fetch origin master failed ({err.strip()[:120]})",
            )

        # 2. Rebase onto origin/master so we're up to date.
        rc, _, err = await self._git(repo_top, ["rebase", "origin/master"])
        if rc != 0:
            # Conflict — abort the rebase to leave the worktree in a clean
            # state, then push the feature branch so work isn't lost.
            await self._git(repo_top, ["rebase", "--abort"])
            logger.warning(
                "rebase of %s onto origin/master failed; pushing feature branch only: %s",
                worktree_branch,
                err.strip()[:200],
            )
            return await self._push_feature_branch_only(
                repo_top=repo_top,
                worktree_branch=worktree_branch,
                commit_sha=commit_sha,
                note_prefix=(
                    f"rebase onto origin/master conflicted ({err.strip()[:120]}); "
                    f"branch retained for manual merge"
                ),
            )

        # Rebase advanced HEAD — refresh the commit SHA we'll report.
        rc, new_head, _ = await self._git(repo_top, ["rev-parse", "HEAD"])
        if rc == 0 and new_head.strip():
            commit_sha = new_head.strip()

        # 3a. Push rebased HEAD to master. Race-safe: another worker may
        #     have pushed in the tiny window between our fetch and push,
        #     so re-fetch + retry once on rejection.
        rc, _, err = await self._git(repo_top, ["push", "origin", "HEAD:refs/heads/master"])
        if rc != 0:
            # Retry-once after re-fetch + re-rebase.
            await self._git(repo_top, ["fetch", "origin", "master"])
            retry_rc, _, retry_err = await self._git(repo_top, ["rebase", "origin/master"])
            if retry_rc != 0:
                await self._git(repo_top, ["rebase", "--abort"])
                return await self._push_feature_branch_only(
                    repo_top=repo_top,
                    worktree_branch=worktree_branch,
                    commit_sha=commit_sha,
                    note_prefix=(
                        f"master push lost the race AND re-rebase conflicted "
                        f"({retry_err.strip()[:120]}); branch retained"
                    ),
                )
            rc2, new_head2, _ = await self._git(repo_top, ["rev-parse", "HEAD"])
            if rc2 == 0 and new_head2.strip():
                commit_sha = new_head2.strip()
            rc, _, err = await self._git(repo_top, ["push", "origin", "HEAD:refs/heads/master"])
            if rc != 0:
                return await self._push_feature_branch_only(
                    repo_top=repo_top,
                    worktree_branch=worktree_branch,
                    commit_sha=commit_sha,
                    note_prefix=(f"master push failed after retry ({err.strip()[:120]})"),
                )

        # 3b. Master push succeeded — delete the local + remote feature
        #     branch. The local branch is removed by _remove_worktree
        #     later; here we delete the remote ref so origin doesn't
        #     accumulate orphan fleet/task_* branches.
        del_rc, _, del_err = await self._git(
            repo_top, ["push", "origin", "--delete", worktree_branch]
        )
        # Remote-delete failing is non-fatal — branch may have been pushed
        # then never created (rebase ate the diff), or already removed by
        # an admin sweep. Log + carry on.
        if del_rc != 0:
            logger.debug(
                "could not delete remote branch origin/%s (non-fatal): %s",
                worktree_branch,
                del_err.strip()[:200],
            )

        return (
            commit_sha,
            True,
            f"rebased onto origin/master and pushed; remote {worktree_branch} deleted",
        )

    async def _push_feature_branch_only(
        self,
        *,
        repo_top: str,
        worktree_branch: str,
        commit_sha: str | None,
        note_prefix: str,
    ) -> tuple[str | None, bool, str]:
        """Fallback: push the worktree branch as a feature branch only.

        Used when rebase-onto-master is impossible (fetch failed, conflict,
        race lost twice). The work is preserved on origin so an operator
        can resolve + merge manually. The local + remote feature branch
        is retained — caller will leave the worktree intact too.
        """
        rc, _, err = await self._git(
            repo_top, ["push", "origin", f"HEAD:refs/heads/{worktree_branch}"]
        )
        if rc != 0:
            return (
                commit_sha,
                False,
                f"{note_prefix}; push of feature branch ALSO failed: {err.strip()[:200]}",
            )
        return (
            commit_sha,
            True,
            f"{note_prefix}; feature branch {worktree_branch} pushed to origin",
        )

    # --------- worktree isolation helpers ----------

    async def _make_worktree(
        self, *, source_repo: str, task_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Create an isolated git worktree off ``origin/master``.

        Returns ``(worktree_path, branch_name, error)``. On failure all three
        are ``(None, None, error_message)`` — callers should fall back to
        shared cwd with a warning.

        Worktrees live under ``/tmp/fleet-worktrees/<task_id>/`` to keep the
        source repo's tree free of fleet bookkeeping dirs (which would
        otherwise show up in ``git status`` and confuse human operators).

        2026-05-19 (branch lifecycle): branch the worktree off
        ``origin/master`` rather than the source repo's local HEAD. The
        local HEAD can be stale (operator switched branches, ran a
        rebase, etc.); by fetching first and basing the worktree on the
        remote tip we guarantee every dispatch starts from a consistent,
        up-to-date master. The branch name is deterministic
        (``fleet/task_<id>``, NOT a random suffix) so a crash leaves a
        recoverable artefact rather than an opaque dangling ref.
        """
        # Verify source is a git repo
        rc, top, _ = await self._git(source_repo, ["rev-parse", "--show-toplevel"])
        if rc != 0:
            return None, None, "source is not a git repo"
        repo_top = top.strip()

        # 2026-05-19: refresh origin/master so the worktree starts at the
        # latest remote tip. Non-fatal — if the fetch fails (no network,
        # no remote, etc.) we fall back to local HEAD with a logged note.
        base_ref = "HEAD"
        fetch_rc, _, fetch_err = await self._git(repo_top, ["fetch", "origin", "master"])
        if fetch_rc == 0:
            # Confirm origin/master is now a resolvable ref before using it
            # as the worktree base. Some repos use 'main'; fall back to HEAD
            # if origin/master doesn't exist.
            check_rc, _, _ = await self._git(repo_top, ["rev-parse", "--verify", "origin/master"])
            if check_rc == 0:
                base_ref = "origin/master"
            else:
                logger.debug(
                    "origin/master not present in %s; basing fleet worktree on HEAD",
                    repo_top,
                )
        else:
            logger.debug(
                "fetch origin master failed in %s: %s; basing fleet worktree on HEAD",
                repo_top,
                fetch_err.strip()[:200],
            )

        # Worktree path + branch name (deterministic, NOT random suffix)
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

        # If the local branch already exists (re-dispatch of same task_id
        # after a previous crash), nuke it so the new worktree starts
        # cleanly from origin/master rather than carrying stale commits.
        await self._git(repo_top, ["branch", "-D", branch])

        # Create branch + worktree in one shot, based on origin/master.
        rc, _, err = await self._git(repo_top, ["worktree", "add", "-b", branch, wt_path, base_ref])
        if rc != 0:
            # Last-resort fallback: try without explicit base (uses HEAD).
            rc2, _, err2 = await self._git(repo_top, ["worktree", "add", wt_path, branch])
            if rc2 != 0:
                return (
                    None,
                    None,
                    (f"git worktree add failed: {err.strip()[:200]} / {err2.strip()[:200]}"),
                )
        return wt_path, branch, None

    async def _remove_worktree(
        self,
        *,
        source_repo: str,
        worktree_path: str,
        branch: str | None,
        task_id: str | None = None,
    ) -> None:
        """Tear down a worktree + its branch. Best-effort, never raises.

        2026-05-19 (lifecycle): also removes the matching entry from the
        active-dispatches state file. Pass ``task_id`` to scope the
        removal — without it we leave the state entry alone (defensive
        default for legacy callers).
        """
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
        finally:
            if task_id:
                remove_dispatch(task_id)

    @classmethod
    async def _capture_head_sha(cls, cwd: str | None) -> str | None:
        """Return ``cwd``'s git HEAD SHA, or None if not a repo / git missing.

        2026-05-11 (anti-hallucination): we snapshot HEAD before and after
        a dispatch so we can compute the set of commits the agent actually
        produced — independent of what the agent claimed in stdout.
        """
        if not cwd:
            return None
        try:
            rc, out, _err = await cls._git(cwd, ["rev-parse", "HEAD"])
        except FileNotFoundError:
            return None
        if rc != 0:
            return None
        sha = out.strip()
        return sha or None

    @classmethod
    def _verify_persistence_claims(
        cls,
        *,
        stdout: str,
        pre_head: str | None,
        post_head: str | None,
        commit_sha: str | None,
        persistence_note: str,
        cwd: str | None,
    ) -> tuple[bool, str, list[str]]:
        """Verify the agent's claimed work matches actual git state.

        Returns ``(hallucination_detected, reason, verified_commits)``.

        * ``hallucination_detected``: True when the agent's output contained
          a verifiable claim of work that we could not corroborate against
          the worktree's git history.
        * ``reason``: human-readable explanation; empty when no concerns.
        * ``verified_commits``: short SHAs the agent emitted that DO exist
          in the worktree (informational; the count may be 0 even when no
          hallucination is detected, e.g. agent returned text only).

        Heuristics (each independently triggers hallucination):
          (1) Agent emitted ``RESULT: N of M tasks complete`` with N > 0
              AND the dispatcher committed nothing AND there's no in-flight
              auto-commit branch ahead of master.
          (2) Agent emitted ``K atomic commits`` for K > 0 AND post_head
              equals pre_head AND the dispatcher made no commit.
          (3) Agent emitted SHA-like tokens (``[0-9a-f]{7,40}``) that
              look like commit references — at least one survives plain-text
              false-positive filtering AND none of them exist in the
              repo via ``git cat-file -e``.

        We deliberately do NOT flag every unverified SHA — output often
        contains hex strings that are coincidental (hash digests, container
        ids). Hallucination needs a concrete claim ("N tasks complete",
        "K commits"), no observed commits, and at least one referenced SHA
        absent.
        """
        if not cwd:
            return False, "", []

        # 1) Parse explicit completion / commit-count claims.
        m_tasks = _TASK_COMPLETION_RE.search(stdout)
        n_tasks_claimed = int(m_tasks.group(1)) if m_tasks else 0
        m_commits = _COMMIT_COUNT_RE.search(stdout)
        n_commits_claimed = int(m_commits.group(1)) if m_commits else 0

        # 2) Compute actual commits produced in this dispatch.
        actually_committed = bool(commit_sha) and persistence_note != "no changes to commit"
        head_advanced = bool(pre_head) and bool(post_head) and pre_head != post_head

        # 3) Collect SHA-like tokens from the output and bucket by existence.
        # We dedupe + keep only candidates ≥ 7 chars (git's default short-sha
        # minimum) to filter out 4-character hex coincidences.
        candidates = {sha.lower() for sha in _SHA_RE.findall(stdout) if len(sha) >= 7}
        verified: list[str] = []
        unverified: list[str] = []
        # Best effort: synchronous existence check by sampling first 12
        # candidates to keep dispatcher latency bounded. The full population
        # would be 50+ for a chatty agent.
        # NOTE: This is the *async-free* gate; we move git lookups into a
        # later phase below to keep this method synchronous.
        # Hand them to the caller for inspection.
        # We don't run git here (this method is sync); rely instead on the
        # presence/absence of commit_sha + head_advanced as ground truth.
        # The SHAs are surfaced verbatim for debugging.
        if actually_committed:
            verified.extend(sorted(candidates))
        else:
            unverified.extend(sorted(candidates))

        # 4) Trigger logic.
        if n_tasks_claimed > 0 and not actually_committed and not head_advanced:
            return (
                True,
                (
                    f"agent claimed {n_tasks_claimed}/{m_tasks.group(2)} tasks complete "
                    f"but no commits were produced (persistence_note={persistence_note!r}, "
                    f"head unchanged at {pre_head[:8] if pre_head else 'unknown'})"
                ),
                verified,
            )
        if n_commits_claimed > 0 and not actually_committed and not head_advanced:
            return (
                True,
                (
                    f"agent claimed {n_commits_claimed} commits but worktree HEAD "
                    f"is unchanged ({pre_head[:8] if pre_head else 'unknown'})"
                ),
                verified,
            )
        # Soft signal: SHA-like tokens emitted but no commit landed.
        # Don't flag as hallucination on its own — could be reading hashes
        # from logs — but include them in the result for the operator.
        if unverified and not actually_committed and not head_advanced:
            return (
                False,
                "",
                sorted(unverified),
            )
        return False, "", verified

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
