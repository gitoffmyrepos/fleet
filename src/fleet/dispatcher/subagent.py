"""Single-subagent dispatcher via the claude CLI in --print mode."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from .base import DispatcherBase
from .claude_args import _claude_args

# 2026-05-11 (opt-8): parse out an explicit RESULT: marker the prompt
# asked the subagent to emit. Matches `^RESULT: <anything>$`, takes the
# LAST such line in stdout (the agent may print partial RESULT-like
# strings in tool output earlier in the run). Falls back to a cleaned
# stdout tail when no RESULT: line is present.
_RESULT_RE = re.compile(r"^RESULT:\s*(.+)$", re.MULTILINE)

# Strip prompt-toolkit / shell noise that leaks into `claude --print`
# stdout at the tail (e.g. `> /exit`, `> > `, ANSI escapes). Drops
# common tokens before the fallback truncation step.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROMPT_NOISE_RE = re.compile(r"(?m)^(?:>\s*/exit\s*|>\s*>\s*|\s*\x1b\[\?\d+[lh])\s*$")


def _clean_stdout_tail(stdout: str, n: int = 2048) -> str:
    """Strip terminal noise + ANSI escapes, return the last n chars."""
    text = _ANSI_RE.sub("", stdout)
    text = _PROMPT_NOISE_RE.sub("", text)
    return text[-n:]


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
        # 2026-05-11 symbiosis-3 / symbiosis-4 plumb-through.
        model: str | None = kwargs.get("model")
        allowed_tools: list[str] | None = kwargs.get("allowed_tools")
        mcp_config_path: str | None = kwargs.get("mcp_config_path")
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
        # 2026-05-11 (opt-8): ask for an explicit summary marker so the
        # parser doesn't have to guess from a truncated tail.
        prompt = (
            f"{prompt}\n\n"
            "When you have your final answer, emit it on a single line "
            "prefixed with `RESULT: ` (no other content on that line)."
        )
        # Prefix the skill catalog header so the subagent knows which Skill
        # invocations are available before reading the task body.
        if skill_header:
            prompt = f"{skill_header}{prompt}"
        return _claude_args(
            self._claude,
            prompt,
            cwd=cwd,
            extra_dirs=skill_roots or None,
            model=model,
            allowed_tools=allowed_tools,
            mcp_config_path=mcp_config_path,
        )

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        """Extract a structured result from the subagent's stdout.

        Priority:
        1. Last `RESULT: <text>` line in stdout (opt-8).
        2. Cleaned stdout tail (last 2 KB minus terminal noise).
        """
        matches = _RESULT_RE.findall(stdout)
        if matches:
            return {"result": matches[-1].strip(), "text_tail": _clean_stdout_tail(stdout)}
        return {"text_tail": _clean_stdout_tail(stdout)}
