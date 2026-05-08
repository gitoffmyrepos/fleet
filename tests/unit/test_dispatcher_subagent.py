from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher.subagent import SubagentDispatcher


@pytest.fixture
def reg() -> CircuitRegistry:
    return CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)


@pytest.fixture
def tel() -> AsyncMock:
    t = AsyncMock()
    for fn in ("start", "end", "failure", "event"):
        setattr(t, fn, AsyncMock(return_value="ep"))
    return t


def test_cli_args_uses_print_flag(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = SubagentDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=300,
        claude_path="/usr/bin/claude",
    )
    args = d.cli_args(task="explain x", agent_hint=None)
    assert args[0] == "/usr/bin/claude"
    assert "--print" in args
    assert any("explain x" in a for a in args)


def test_cli_args_passes_agent_hint(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = SubagentDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=300,
        claude_path="/usr/bin/claude",
    )
    args = d.cli_args(task="explain x", agent_hint="superpowers:tdd-guide")
    joined = " ".join(args)
    assert "tdd-guide" in joined or "agent" in joined.lower()


def test_parse_summary_returns_truncated_tail(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = SubagentDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=300,
        claude_path="/usr/bin/claude",
    )
    summary = d.parse_summary("x" * 5000, task="any")
    assert "text_tail" in summary
    assert len(summary["text_tail"]) <= 2048


def test_upstream_name_is_superpowers(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = SubagentDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=300,
        claude_path="/usr/bin/claude",
    )
    assert d.upstream_name == "superpowers"
