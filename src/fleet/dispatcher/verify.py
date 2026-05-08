"""Verification dispatcher invoking superpowers verification-before-completion."""

from __future__ import annotations

import re
from typing import Any, ClassVar

from .base import DispatcherBase

_VERDICT_RE = re.compile(r"VERDICT:\s*(PASS|FAIL|UNKNOWN)", re.IGNORECASE)


class VerifyDispatcher(DispatcherBase):
    """Run the superpowers `verification-before-completion` skill via `claude --print`.

    cli_args kwargs:
        task (str, required): description of what to verify
        scope (str | None, optional): a path or area to focus verification on

    parse_summary returns `{"verdict": "PASS"|"FAIL"|"UNKNOWN", "stdout_tail": str}`.
    A missing or unrecognized VERDICT line yields `"UNKNOWN"`.
    """

    upstream_name: ClassVar[str] = "superpowers"

    def __init__(self, *, claude_path: str, **kw: Any) -> None:
        super().__init__(**kw)
        self._claude = claude_path

    def cli_args(self, **kwargs: Any) -> list[str]:
        task: str = kwargs["task"]
        scope: str | None = kwargs.get("scope")
        prompt = f"Use the verification-before-completion skill to verify: {task}"
        if scope:
            prompt += f" Scope: {scope}"
        return [self._claude, "--print", "--output-format", "text", prompt]

    def parse_summary(self, stdout: str, stderr: str = "", **kwargs: Any) -> dict[str, Any]:
        m = _VERDICT_RE.search(stdout)
        verdict = m.group(1).upper() if m else "UNKNOWN"
        return {"verdict": verdict, "stdout_tail": stdout[-1024:]}
