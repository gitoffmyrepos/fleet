"""Swarm dispatcher wrapping claude-flow swarm + hive-mind."""

from __future__ import annotations

import os
import re
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
        task: str = kwargs["task"]
        agents: int = int(kwargs.get("agents", 20))
        topology: str = kwargs.get("topology", "parallel")
        strategy: str = kwargs.get("strategy", "development")
        if topology in ("hive-mind", "hierarchical"):
            return [self._cli, "hive-mind", "spawn", task, "--agents", str(agents)]
        return [
            self._cli,
            "swarm",
            "start",
            "-o",
            task,
            "-s",
            strategy,
            "--agents",
            str(agents),
        ]

    def env(self, **kwargs: Any) -> dict[str, str] | None:
        e = dict(os.environ)
        e["FLEET_INVOKED"] = "1"
        return e

    def parse_summary(self, stdout: str, **kwargs: Any) -> dict[str, Any]:
        agents = int(kwargs.get("agents", 20))
        m = _RESULT_RE.search(stdout)
        result = m.group(1).strip() if m else stdout[-1024:]
        return {"agents_used": agents, "result": result[:2048]}
