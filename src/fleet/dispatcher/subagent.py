"""Single-subagent dispatcher via the claude CLI in --print mode."""

from __future__ import annotations

from typing import Any, ClassVar

from .base import DispatcherBase
from .claude_args import _claude_args


class SubagentDispatcher(DispatcherBase):
    upstream_name: ClassVar[str] = "superpowers"

    def __init__(self, *, claude_path: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._claude = claude_path

    def cli_args(self, **kwargs: Any) -> list[str]:
        task: str = kwargs["task"]
        agent_hint: str | None = kwargs.get("agent_hint")
        cwd: str | None = kwargs.get("cwd")
        skill_header: str = kwargs.get("skill_header") or ""
        skill_roots: list[str] = kwargs.get("skill_roots") or []
        prompt = task
        if agent_hint:
            prompt = f"Use agent {agent_hint}. {task}"
        if cwd:
            # Tell the agent where its work belongs and that it must persist.
            prompt = (
                f"{prompt}\n\n"
                f"Your working directory is {cwd}. All file changes go there. "
                f"When the task is complete, your work is automatically committed and "
                f"pushed by Fleet — you do not need to run git commit/push yourself."
            )
        # Prefix the skill catalog header so the subagent knows which Skill
        # invocations are available before reading the task body.
        if skill_header:
            prompt = f"{skill_header}{prompt}"
        return _claude_args(self._claude, prompt, cwd=cwd, extra_dirs=skill_roots or None)

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        return {"text_tail": stdout[-2048:]}
