from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher.swarm import SwarmDispatcher

FIX = Path(__file__).parent.parent / "fixtures" / "cli_outputs" / "ruflo_swarm_ok.txt"


@pytest.fixture
def reg() -> CircuitRegistry:
    return CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)


@pytest.fixture
def tel() -> AsyncMock:
    t = AsyncMock()
    for fn in ("start", "end", "failure", "event"):
        setattr(t, fn, AsyncMock(return_value="ep"))
    return t


def test_cli_args_default(reg: CircuitRegistry, tel: AsyncMock) -> None:
    """All topologies route through `claude-flow hive-mind spawn --claude` because
    `claude-flow swarm start` is a stub that prints a table without spawning real
    agents (see SwarmDispatcher.cli_args docstring). Test asserts the actual
    contract, not the historical/aspirational `swarm start` form.
    """
    d = SwarmDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        cli_path="/usr/bin/claude-flow",
        workdir="/tmp/wd",
    )
    args = d.cli_args(task="audit svcs", agents=20, topology="parallel", strategy="development")
    assert "hive-mind" in args and "spawn" in args and "--claude" in args
    assert "-o" in args and "audit svcs" in args
    assert "-n" in args and "20" in args
    assert "-s" in args and "development" in args
    assert "--workdir" in args and "/tmp/wd" in args


@pytest.mark.parametrize("topology", ["hive-mind", "hierarchical"])
def test_cli_args_hive_mind_routes_for_both_topology_aliases(
    topology: str, reg: CircuitRegistry, tel: AsyncMock
) -> None:
    d = SwarmDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        cli_path="/usr/bin/claude-flow",
        workdir="/tmp/wd",
    )
    args = d.cli_args(task="diagnose", agents=10, topology=topology, strategy="analysis")
    assert "hive-mind" in args and "spawn" in args


def test_env_merges_os_environ_and_sets_fleet_invoked(
    reg: CircuitRegistry, tel: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXISTING_VAR", "x")
    d = SwarmDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        cli_path="/usr/bin/claude-flow",
        workdir="/tmp/wd",
    )
    e = d.env()
    assert e is not None
    assert e["EXISTING_VAR"] == "x"
    assert e["FLEET_INVOKED"] == "1"


def test_parse_summary_extracts_result_and_agent_count(
    reg: CircuitRegistry, tel: AsyncMock
) -> None:
    d = SwarmDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        cli_path="/usr/bin/claude-flow",
        workdir="/tmp/wd",
    )
    summary = d.parse_summary(FIX.read_text(), task="audit", agents=20)
    assert summary["agents_used"] == 20
    assert "All 73 services healthy" in summary["result"]


def test_upstream_name_is_ruflo(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = SwarmDispatcher(
        circuits=reg,
        telemetry=tel,
        timeout_seconds=1800,
        cli_path="/usr/bin/claude-flow",
        workdir="/tmp/wd",
    )
    assert d.upstream_name == "ruflo"
