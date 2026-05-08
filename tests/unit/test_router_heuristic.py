import json
from pathlib import Path

import pytest

from fleet.router import HeuristicGate

CASES = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "router_cases" / "cases.json").read_text()
)


@pytest.fixture
def gate() -> HeuristicGate:
    return HeuristicGate()


@pytest.mark.parametrize("case", CASES, ids=[c["task"][:30] for c in CASES])
def test_heuristic_gate_classifies_known_cases(gate: HeuristicGate, case: dict[str, str]) -> None:
    decision = gate.classify(case["task"])
    assert decision.kind == case["expected_kind"]
    assert 0.0 <= decision.confidence <= 1.0


def test_inconclusive_returns_low_confidence(gate: HeuristicGate) -> None:
    decision = gate.classify("hi how are you")
    assert decision.confidence < 0.5


def test_empty_task_safe_default(gate: HeuristicGate) -> None:
    decision = gate.classify("")
    assert decision.kind == "subagent"
    assert decision.confidence < 0.5
