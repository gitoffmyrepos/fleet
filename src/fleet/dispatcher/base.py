"""Shared subprocess-runner with circuit guard + telemetry."""

from __future__ import annotations

import abc
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, ClassVar

from ..circuit import CircuitOpen, CircuitRegistry
from ..telemetry import Telemetry


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

    def parse_summary(self, stdout: str, **kwargs: Any) -> dict[str, Any]:
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
        await self._t.start(
            task_id=task_id,
            kind=self.upstream_name,
            body={"args": args[:6]},
        )
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env(**kwargs),
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
        summary = self.parse_summary(stdout, **kwargs)
        await self._t.end(task_id=task_id, ok=True, body={"summary": summary, "exit_code": 0})
        return DispatchResult(
            ok=True,
            task_id=task_id,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            exit_code=0,
            duration_seconds=time.monotonic() - t0,
        )
