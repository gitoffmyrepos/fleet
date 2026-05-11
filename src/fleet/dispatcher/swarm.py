"""Swarm dispatcher wrapping claude-flow swarm + hive-mind."""

from __future__ import annotations

import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any, ClassVar

from .base import DispatcherBase

logger = logging.getLogger(__name__)

_RESULT_RE = re.compile(r"^RESULT:\s*(.+)$", re.MULTILINE)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 2026-05-11 (opt-4): persist captured stdout/stderr to ~/.local/state/fleet/swarms/
# so operators can inspect long-form output post-dispatch instead of
# grepping the systemd journal. parse_summary returns the path alongside
# the extracted RESULT: line.
_SWARM_LOG_DIR = Path.home() / ".local" / "state" / "fleet" / "swarms"


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

        CRITICAL: --workdir is REQUIRED for file-writing tasks. Without it, spawned
        agents use their own workspace (default ruflo dir) and changes are never written
        to the target project. This caused the 2026-05-10 Monty integration failure
        where 3/4 microservices had zero changes despite swarms completing successfully.

        2026-05-11 (opt-4): when ``script(1)`` is available we wrap the
        claude-flow invocation with ``script -qfc`` so the spawned
        ``claude --claude`` children see a pseudo-TTY on stdout. Some
        CLIs (claude-flow included) detect ``isatty(stdout) is False``
        and trigger noninteractive paths that omit progress output —
        the PTY tricks them into emitting normally so parse_summary
        has real content to extract a ``RESULT:`` line from.
        """
        task: str = kwargs["task"]
        agents: int = int(kwargs.get("agents", 20))
        strategy: str = kwargs.get("strategy", "development")
        workdir: str = kwargs.get("workdir", self._wd)  # pass project dir explicitly
        skill_header: str = kwargs.get("skill_header") or ""
        # Prepend the skill catalog header so each spawned hive-mind agent
        # knows which Skill tools are available before reading the task.
        # (Skill roots are exposed via --add-dir at the per-agent level
        # only for subagent dispatch; hive-mind uses --workdir + its own
        # roots discovery.)
        objective = f"{skill_header}{task}" if skill_header else task
        inner = [
            self._cli,
            "hive-mind",
            "spawn",
            "--claude",
            "-n",
            str(agents),
            "-s",
            strategy,
            "-o",
            objective,
            "--workdir",
            workdir,
        ]
        # 2026-05-11 (opt-4): PTY shim. Falls back to direct invocation
        # if `script` is missing.
        script_bin = shutil.which("script")
        if not script_bin:
            return inner
        # `script -qfc "<cmd>" /dev/null` runs <cmd> inside a pty;
        # quiet, flush-each-write, capture-to-stdout (we still capture
        # via base.py's PIPE, the /dev/null arg just disables the
        # `typescript` file). Quoting the inner command preserves arg
        # boundaries through script(1)'s sh-style interpretation.
        import shlex

        inner_str = " ".join(shlex.quote(a) for a in inner)
        return [script_bin, "-qfc", inner_str, "/dev/null"]

    def env(self, **kwargs: Any) -> dict[str, str]:
        """Return full env with PWD and KUBECONFIG set to project dir.

        asyncio.create_subprocess_exec with env= replaces the process env entirely,
        so we must explicitly include PWD (sets Claude Code's CWD so it discovers
        CLAUDE.md in the target project) and KUBECONFIG.

        The PWD trick: when `workdir` is a subdir of self._wd (ruflo's workspace),
        we set PWD to workdir so spawned `claude` processes use it as CWD and
        find CLAUDE.md there. Without this, Claude uses self._wd as CWD, finds
        the ruflo CLAUDE.md instead of the project's, and works in isolation.
        This caused the 2026-05-10 Monty integration failure where 3/4
        microservices had zero disk changes despite swarms completing.
        """
        e = dict(os.environ)
        e["FLEET_INVOKED"] = "1"
        workdir: str = kwargs.get("workdir", self._wd)
        e["PWD"] = workdir
        if "KUBECONFIG" not in e:
            kube_config = Path.home() / ".kube" / "config"
            if kube_config.exists():
                e["KUBECONFIG"] = str(kube_config)
        return e

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        """Extract RESULT: line + persist captured output to a log file.

        2026-05-11 (opt-4):
        * Search the FULL stdout for the last ``RESULT:`` line (claude-flow
          can emit thousands of lines; the trailing 4 KB window often
          missed it).
        * Persist the captured stdout/stderr to
          ``~/.local/state/fleet/swarms/<task_id>.log`` (with ANSI escapes
          stripped) so the operator can recover the full transcript.
        * Falls back to a generic message only when no RESULT: marker is
          found anywhere — that's a signal the swarm didn't follow the
          contract, not a "nothing to see here" placeholder.
        """
        agents = int(kwargs.get("agents", 20))
        task_id: str = kwargs.get("task_id") or "untracked"

        # Persist captured output before parsing so a parse exception
        # doesn't lose the transcript.
        log_path: Path | None = None
        try:
            _SWARM_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_path = _SWARM_LOG_DIR / f"{task_id}.log"
            log_path.write_text(
                _ANSI_RE.sub("", stdout) + "\n--- STDERR ---\n" + _ANSI_RE.sub("", stderr),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("could not persist swarm log to %s: %s", log_path, exc)

        # Scan FULL stdout (not just the tail) for the last RESULT: line.
        # Falls back to stderr if claude-flow re-routed its summary there.
        result = ""
        for source in (stdout, stderr):
            matches = _RESULT_RE.findall(source)
            if matches:
                result = matches[-1].strip()
                break

        if not result:
            log_hint = f" (full log: {log_path})" if log_path else ""
            result = (
                f"hive-mind spawn dispatched {agents} agents; no RESULT: line "
                f"was emitted{log_hint}."
            )

        return {
            "agents_used": agents,
            "result": result[:2048],
            "log_path": str(log_path) if log_path else None,
        }
