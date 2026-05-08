"""Phase dispatcher driving the gsd lifecycle."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from .base import DispatcherBase

_STAGE_TO_CMD: dict[str, str] = {
    "plan": "/gsd:plan-phase",
    "execute": "/gsd:execute-phase",
    "verify": "/gsd:verify-work",
    "discuss": "/gsd:discuss-phase",
    "ship": "/gsd:ship",
}
_PHASE_DIR_RE = re.compile(r"PHASE_DIR=([\w./-]+)")


class PhaseDispatcher(DispatcherBase):
    """Wrap the gsd lifecycle (plan/execute/verify/discuss/ship) via `claude --print`.

    Stages map to gsd slash commands:
        plan    → /gsd:plan-phase
        execute → /gsd:execute-phase
        verify  → /gsd:verify-work
        discuss → /gsd:discuss-phase
        ship    → /gsd:ship
    Unknown stage values fall back to /gsd:plan-phase.
    """

    upstream_name: ClassVar[str] = "gsd"

    def __init__(self, *, claude_path: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._claude = claude_path

    def cli_args(self, **kwargs: Any) -> list[str]:
        task: str = kwargs["task"]
        stage: str = kwargs.get("stage", "plan")
        cmd = _STAGE_TO_CMD.get(stage, "/gsd:plan-phase")
        return [self._claude, "--print", "--output-format", "text", f"{cmd} {task}"]

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        stage: str = kwargs.get("stage", "plan")
        m = _PHASE_DIR_RE.search(stdout)
        phase_dir: str | None = m.group(1) if m else None
        return {
            "stage": stage,
            "phase_dir": phase_dir,
            "stdout_tail": stdout[-1024:],
        }
