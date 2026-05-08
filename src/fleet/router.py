"""Two-stage router: heuristic gate first, LLM fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

from .config import Settings
from .telemetry import Telemetry


class _MessagesAPI(Protocol):
    async def create(self, **kwargs: Any) -> Any: ...


class _AnthropicLike(Protocol):
    @property
    def messages(self) -> _MessagesAPI: ...


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


_LLM_PROMPT = """You are a task router. Classify the task below into exactly one kind:
- "swarm": parallel fan-out across many similar items (audits, scans, surveys)
- "phase": multi-step build/refactor that benefits from plan + execute + verify
- "subagent": a single Q&A or focused investigation
- "verify": gate-checking that something works
- "ship": release / deploy / merge / tag

Return ONLY JSON: {"kind":"...","confidence":0.0-1.0,"reason":"..."}
TASK: %s
"""


class Router:
    def __init__(
        self,
        *,
        settings: Settings,
        anthropic: _AnthropicLike,
        telemetry: Telemetry,
    ) -> None:
        self._cfg = settings
        self._a = anthropic
        self._t = telemetry
        self._heuristic = HeuristicGate()

    async def route(self, *, task: str, task_id: str) -> RouteDecision:
        h = self._heuristic.classify(task)
        if h.confidence >= self._cfg.router_confidence_threshold:
            decision = h
        else:
            decision = await self._call_llm(task, h)
        await self._t.event(
            task_id=task_id,
            kind="fleet_route_decision",
            body={
                "kind": decision.kind,
                "confidence": decision.confidence,
                "via": decision.via,
                "degraded": decision.degraded,
                "reason": decision.reason[:200],
            },
        )
        return decision

    async def _call_llm(self, task: str, heuristic: RouteDecision) -> RouteDecision:
        if self._a is None:
            return self._safe_fallback(heuristic, "llm not configured", degraded=True)
        try:
            msg = await self._a.messages.create(
                model=self._cfg.router_model,
                max_tokens=200,
                messages=[{"role": "user", "content": _LLM_PROMPT % task[:1000]}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content)
            payload = self._parse_payload(text)
            if payload is None:
                return self._safe_fallback(heuristic, "llm returned non-json", degraded=True)
            kind = payload.get("kind", "subagent")
            if kind not in {"swarm", "phase", "subagent", "verify", "ship"}:
                return self._safe_fallback(heuristic, f"llm invalid kind {kind}", degraded=True)
            return RouteDecision(
                kind=kind,
                confidence=float(payload.get("confidence", 0.6)),
                reason=str(payload.get("reason", ""))[:400],
                via="llm",
            )
        except Exception as e:
            return self._safe_fallback(heuristic, f"llm error: {type(e).__name__}", degraded=True)

    @staticmethod
    def _parse_payload(text: str) -> dict[str, Any] | None:
        s = text.strip()
        i = s.find("{")
        j = s.rfind("}")
        if i < 0 or j <= i:
            return None
        # `s[i:j+1]` always starts with `{` and ends with `}`, so json.loads
        # either returns a dict or raises — no non-dict path possible here.
        try:
            return json.loads(s[i : j + 1])  # type: ignore[no-any-return]
        except json.JSONDecodeError:
            return None

    def _safe_fallback(
        self, heuristic: RouteDecision, reason: str, *, degraded: bool
    ) -> RouteDecision:
        if heuristic.confidence >= self._cfg.router_safe_fallback_threshold:
            return RouteDecision(
                kind=heuristic.kind,
                confidence=heuristic.confidence,
                reason=reason,
                via="fallback",
                degraded=degraded,
            )
        return RouteDecision(
            kind="subagent",
            confidence=0.3,
            reason=reason,
            via="fallback",
            degraded=degraded,
        )
