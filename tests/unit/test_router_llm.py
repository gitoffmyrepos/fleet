from unittest.mock import AsyncMock, MagicMock

import pytest

from fleet.config import Settings
from fleet.router import Router


@pytest.fixture
def settings() -> Settings:
    return Settings(
        anthropic_api_key="sk-test",
        router_model="claude-sonnet-4-6",
        router_confidence_threshold=0.7,
        router_safe_fallback_threshold=0.5,
        _env_file=None,  # type: ignore[call-arg]
    )


def make_anthropic_response(payload: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(type="text", text=payload)]
    return msg


@pytest.mark.asyncio
async def test_high_heuristic_confidence_skips_llm(settings: Settings) -> None:
    anthropic = AsyncMock()
    anthropic.messages.create = AsyncMock()
    tel = AsyncMock()
    r = Router(settings=settings, anthropic=anthropic, telemetry=tel)
    decision = await r.route(task="audit all 73 microservices in parallel", task_id="t1")
    assert decision.via == "heuristic"
    assert decision.kind == "swarm"
    anthropic.messages.create.assert_not_called()


@pytest.mark.asyncio
async def test_low_heuristic_confidence_calls_llm(settings: Settings) -> None:
    anthropic = AsyncMock()
    anthropic.messages.create = AsyncMock(
        return_value=make_anthropic_response(
            '{"kind":"phase","confidence":0.85,"reason":"multi-step build"}'
        )
    )
    tel = AsyncMock()
    r = Router(settings=settings, anthropic=anthropic, telemetry=tel)
    decision = await r.route(task="hi how are you doing today", task_id="t2")
    assert decision.via == "llm"
    assert decision.kind == "phase"
    anthropic.messages.create.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_unreachable_falls_back_to_heuristic_with_degraded_flag(
    settings: Settings,
) -> None:
    anthropic = AsyncMock()
    anthropic.messages.create = AsyncMock(side_effect=ConnectionError("boom"))
    tel = AsyncMock()
    r = Router(settings=settings, anthropic=anthropic, telemetry=tel)
    decision = await r.route(task="hi friend", task_id="t3")
    assert decision.degraded is True
    assert decision.via == "fallback"
    assert decision.kind == "subagent"


@pytest.mark.asyncio
async def test_llm_returns_garbage_safe_default(settings: Settings) -> None:
    anthropic = AsyncMock()
    anthropic.messages.create = AsyncMock(return_value=make_anthropic_response("not json at all"))
    tel = AsyncMock()
    r = Router(settings=settings, anthropic=anthropic, telemetry=tel)
    decision = await r.route(task="hi friend", task_id="t4")
    assert decision.kind == "subagent"
    assert decision.via == "fallback"


@pytest.mark.asyncio
async def test_route_emits_telemetry(settings: Settings) -> None:
    anthropic = AsyncMock()
    tel = AsyncMock()
    tel.event = AsyncMock(return_value="ep")
    r = Router(settings=settings, anthropic=anthropic, telemetry=tel)
    await r.route(task="audit all 73 microservices in parallel", task_id="t5")
    tel.event.assert_awaited()
    call = tel.event.await_args
    assert call.kwargs["kind"] == "fleet_route_decision"
