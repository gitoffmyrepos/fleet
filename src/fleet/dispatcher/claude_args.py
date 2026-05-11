"""Shared `claude --print` arg builder for Fleet dispatchers.

Centralises the permission flags and tool whitelist so Fleet-spawned agents
have the headless-mode access they need to actually do work (Bash, Write,
Edit, Task, etc.) without prompt-loops, and so file changes land in the
caller's project directory rather than Fleet's daemon CWD.
"""

from __future__ import annotations

# Tools we whitelist for ALL Fleet-spawned agents. The list mirrors what an
# interactive Claude Code session has by default. Bash is the load-bearing
# entry — without it the agent can't run tests, commits, kubectl, anything.
#
# We pass these as space-separated to satisfy claude --allowedTools' parser
# (it accepts both comma- and space-separated; space is unambiguous).
_DEFAULT_ALLOWED_TOOLS: list[str] = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "Task",
    "WebFetch",
    "WebSearch",
    "TodoWrite",
    "NotebookEdit",
    "BashOutput",
    "KillBash",
    "SlashCommand",
]


def _claude_args(
    claude_path: str,
    prompt: str,
    *,
    cwd: str | None = None,
    extra_dirs: list[str] | None = None,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    """Build a `claude --print` argv list with the right permissions.

    - When cwd is provided, it's added via --add-dir so the agent can read/
      write files there. (The subprocess CWD is set separately by base.py
      via create_subprocess_exec(cwd=...) — --add-dir grants permission, cwd
      sets the actual working directory.)
    - --permission-mode acceptEdits: Edit/Write actions auto-approved (no
      interactive prompt that would hang in --print mode).
    - --allowedTools whitelist gives Bash + filesystem + web access without
      the nuclear --dangerously-skip-permissions option.
    """
    tools = allowed_tools if allowed_tools is not None else _DEFAULT_ALLOWED_TOOLS
    args = [claude_path, "--print", "--output-format", "text"]

    if cwd:
        args.extend(["--add-dir", cwd])
    if extra_dirs:
        for d in extra_dirs:
            args.extend(["--add-dir", d])

    args.extend(["--allowedTools", *tools])
    args.extend(["--permission-mode", "acceptEdits"])

    args.append(prompt)
    return args
