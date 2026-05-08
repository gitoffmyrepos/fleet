from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher.verify import VerifyDispatcher


@pytest.fixture
def reg() -> CircuitRegistry:
    return CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)


@pytest.fixture
def tel() -> AsyncMock:
    t = AsyncMock()
    for fn in ("start", "end", "failure", "event"):
        setattr(t, fn, AsyncMock(return_value="ep"))
    return t


def test_cli_args_invokes_skill(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = VerifyDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=900,
        claude_path="/usr/bin/claude",
    )
    args = d.cli_args(task="check x works", scope=None)
    s = " ".join(args)
    assert "verification-before-completion" in s
    assert "check x works" in s


def test_cli_args_includes_scope(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = VerifyDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=900,
        claude_path="/usr/bin/claude",
    )
    args = d.cli_args(task="t", scope="src/auth/")
    assert "src/auth/" in " ".join(args)


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ("VERDICT: PASS\nall checks green\n", "PASS"),
        ("VERDICT: FAIL\nsomething broke\n", "FAIL"),
        ("verdict: pass\n", "PASS"),  # case-insensitive
        ("ran tests\n", "UNKNOWN"),  # no verdict line
    ],
)
def test_parse_summary_returns_verdict_field(
    stdout: str, expected: str, reg: CircuitRegistry, tel: AsyncMock
) -> None:
    d = VerifyDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=900,
        claude_path="/usr/bin/claude",
    )
    summary = d.parse_summary(stdout, task="t")
    assert summary["verdict"] == expected


def test_upstream_name_is_superpowers(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = VerifyDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=900,
        claude_path="/usr/bin/claude",
    )
    assert d.upstream_name == "superpowers"
