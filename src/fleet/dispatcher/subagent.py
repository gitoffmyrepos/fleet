"""Single-subagent dispatcher via the claude CLI in --print mode."""

from __future__ import annotations

from typing import Any, ClassVar

from .base import DispatcherBase


class SubagentDispatcher(DispatcherBase):
    upstream_name: ClassVar[str] = "superpowers"

    def __init__(self, *, claude_path: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._claude = claude_path

    def cli_args(self, **kwargs: Any) -> list[str]:
        task: str = kwargs["task"]
        agent_hint: str | None = kwargs.get("agent_hint")
        prompt = task
        if agent_hint:
            prompt = f"Use agent {agent_hint}. {task}"
        return [self._claude, "--print", "--output-format", "text", prompt]

    def parse_summary(self, stdout: str, **kwargs: Any) -> dict[str, Any]:
        return {"text_tail": stdout[-2048:]}
