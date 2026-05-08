"""Two-stage router: heuristic gate first, LLM fallback."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class RouteDecision:
    kind: str  # swarm | phase | subagent | verify | ship
    confidence: float
    reason: str
    via: str = "heuristic"  # heuristic | llm | fallback
    suggested_agents: int | None = None
    suggested_topology: str | None = None
    degraded: bool = False


_SWARM_PATTERNS = [
    r"\ball \d+\b",
    r"\bevery \d+\b",
    r"\bspawn \d+\b",
    r"\b\d+ services?\b",
    r"\baudit\b",
    r"\bscan\b",
    r"\bsurvey\b",
    r"\binventory\b",
    r"\bin parallel\b",
    r"\bfan[\s-]out\b",
]
_PHASE_PATTERNS = [
    r"\bimplement\b",
    r"\bbuild\b",
    r"\badd (the |a |an )?\w+",
    r"\bfeature\b",
    r"\brefactor\b",
    r"\brewrite\b",
    r"\bL\d+\b",
    r"\bphase\b",
]
_VERIFY_PATTERNS = [
    r"\bverify\b",
    r"\bvalidate\b",
    r"\bcheck (that|if|whether)\b",
    r"\bdoes (it|this) work\b",
]
_SHIP_PATTERNS = [
    r"\bship\b",
    r"\brelease\b",
    r"\bdeploy\b",
    r"\bmerge( and| then)?\b",
    r"\btag v\d",
    r"\bcut (a |the )?release\b",
]
_SUBAGENT_PATTERNS = [
    r"\bexplain\b",
    r"\bdescribe\b",
    r"\bwhat is\b",
    r"\bhow does\b",
    r"\bsummari[sz]e\b",
    r"\bfind (the |where)\b",
]


def _hits(text: str, pats: list[str]) -> int:
    return sum(1 for p in pats if re.search(p, text))


class HeuristicGate:
    def classify(self, task: str) -> RouteDecision:
        t = task.strip().lower()
        if not t:
            return RouteDecision(kind="subagent", confidence=0.0, reason="empty input safe default")
        scores = {
            "swarm": _hits(t, _SWARM_PATTERNS) * 0.35,
            "phase": _hits(t, _PHASE_PATTERNS) * 0.30,
            "verify": _hits(t, _VERIFY_PATTERNS) * 0.40,
            "ship": _hits(t, _SHIP_PATTERNS) * 0.45,
            "subagent": _hits(t, _SUBAGENT_PATTERNS) * 0.30,
        }
        kind, score = max(scores.items(), key=lambda x: x[1])
        confidence = min(score, 0.95)
        if confidence < 0.25:
            return RouteDecision(
                kind="subagent", confidence=confidence, reason="no strong heuristic match"
            )
        return RouteDecision(
            kind=kind,
            confidence=confidence,
            reason=f"heuristic match for {kind}",
            suggested_agents=20 if kind == "swarm" else None,
            suggested_topology="parallel" if kind == "swarm" else None,
        )
