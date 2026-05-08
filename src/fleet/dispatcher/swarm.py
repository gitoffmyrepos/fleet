"""Swarm dispatcher wrapping claude-flow swarm + hive-mind."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, ClassVar

from .base import DispatcherBase

_RESULT_RE = re.compile(r"^RESULT:\s*(.+)$", re.MULTILINE)


class SwarmDispatcher(DispatcherBase):
    upstream_name: ClassVar[str] = "ruflo"

    def __init__(self, *, cli_path: str, workdir: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._cli = cli_path
        self._wd = workdir

    def cli_args(self, **kwargs: Any) -> list[str]:
        """Build claude-flow CLI args.

        Topology routing:
          - 'hive-mind' / 'hierarchical' → `claude-flow hive-mind spawn --claude ...`
            (hive-mind is the only command that actually spawns real Claude Code agents)
          - all others (parallel / mesh / star / ring) → also route to hive-mind spawn
            because `claude-flow swarm start` is a stub that only prints a table and exits.
        """
        task: str = kwargs["task"]
        agents: int = int(kwargs.get("agents", 20))
        strategy: str = kwargs.get("strategy", "development")
        # Always use hive-mind spawn --claude — it calls child_process.spawn('claude', ...)
        # which is the only CLI command that actually spawns real agents.
        # swarm start is a stub (only prints a table, no subprocess spawning).
        return [
            self._cli,
            "hive-mind",
            "spawn",
            "--claude",
            "-n",
            str(agents),
            "-s",
            strategy,
            "-o",
            task,
        ]

    def env(self, **kwargs: Any) -> dict[str, str]:
        """Return full env with KUBECONFIG preserved.

        asyncio.create_subprocess_exec with env= replaces the process env entirely,
        so we must include KUBECONFIG explicitly (copy from current env).
        """
        e = dict(os.environ)
        e["FLEET_INVOKED"] = "1"
        # Ensure kubectl can reach the cluster — KUBECONFIG may be in parent shell
        # but we must explicitly include it since we're replacing the whole env.
        if "KUBECONFIG" not in e:
            kube_config = Path.home() / ".kube" / "config"
            if kube_config.exists():
                e["KUBECONFIG"] = str(kube_config)
        return e

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        agents = int(kwargs.get("agents", 20))
        # hive-mind spawn --claude uses stdio: inherit so stdout goes to TTY,
        # not captured. Fall back to stderr which may contain result info.
        # The actual agent work happens in the spawned claude process.
        combined = (stdout + stderr)[-4096:]
        m = _RESULT_RE.search(combined)
        result = m.group(1).strip() if m else ""
        if not result:
            result = f"hive-mind spawn dispatched {agents} agents. Check cluster for work."
        return {"agents_used": agents, "result": result[:2048]}
