from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher.phase import PhaseDispatcher

FIX = Path(__file__).parent.parent / "fixtures" / "cli_outputs"


@pytest.fixture
def reg() -> CircuitRegistry:
    return CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)


@pytest.fixture
def tel() -> AsyncMock:
    t = AsyncMock()
    for fn in ("start", "end", "failure", "event"):
        setattr(t, fn, AsyncMock(return_value="ep"))
    return t


def test_cli_args_uses_slash_command(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = PhaseDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        claude_path="/usr/bin/claude",
    )
    args = d.cli_args(task="add SSE event", stage="plan")
    joined = " ".join(args)
    assert "/gsd:plan-phase" in joined
    assert "add SSE event" in joined


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("plan", "/gsd:plan-phase"),
        ("execute", "/gsd:execute-phase"),
        ("verify", "/gsd:verify-work"),
    ],
)
def test_cli_args_for_each_stage(
    stage: str, expected: str, reg: CircuitRegistry, tel: AsyncMock
) -> None:
    d = PhaseDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        claude_path="/usr/bin/claude",
    )
    args = d.cli_args(task="x", stage=stage)
    assert expected in " ".join(args)


def test_parse_summary_extracts_phase_dir(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = PhaseDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        claude_path="/usr/bin/claude",
    )
    summary = d.parse_summary((FIX / "gsd_plan_ok.txt").read_text(), stage="plan", task="x")
    assert summary["phase_dir"] is not None
    assert summary["phase_dir"].endswith("2026-05-07-add-sse-event/")
    assert summary["stage"] == "plan"


def test_upstream_name_is_gsd(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = PhaseDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        claude_path="/usr/bin/claude",
    )
    assert d.upstream_name == "gsd"
