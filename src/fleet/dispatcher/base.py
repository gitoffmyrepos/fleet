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

        args = self.cli_args(**kwargs)
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
        await self._t.start(
            task_id=task_id,
            kind=self.upstream_name,
            body={"args": args[:6], "cwd": cwd or os.getcwd()},
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env(**kwargs),
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
            return DispatchResult(
                ok=False,
                task_id=task_id,
                stdout=stdout,
                stderr=stderr,
                error=err,
                exit_code=proc.returncode,
                duration_seconds=time.monotonic() - t0,
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
        )

    async def _commit_and_push(
        self,
        *,
        cwd: str,
        task_id: str,
        task: str,
    ) -> tuple[str | None, bool, str]:
        """Commit working-tree changes + push the current branch.

        Returns (commit_sha, pushed, note). Never raises — persistence is
        best-effort; the agent's work already happened on disk.

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

        # 5. Push current branch to origin.
        rc, _, err = await self._git(repo_top, ["push", "origin", "HEAD"])
        if rc != 0:
            return commit_sha, False, f"committed locally but push failed: {err.strip()[:200]}"

        return commit_sha, True, "committed and pushed"

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
