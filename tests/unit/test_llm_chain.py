"""SP-F (2026-05-24) — unit tests for fleet.llm.LLMChain.

Mocks adapter functions to test fallback transitions without burning
real API calls.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from fleet.llm.provider_chain import (
    LLMChain,
    LLMChainExhaustedError,
)
from fleet.llm.providers import (
    ProviderAuthError,
    ProviderPermanentError,
    ProviderTransientError,
)


def _make_chain(adapters: dict[str, Any], keys: dict[str, str] | None = None) -> LLMChain:
    """Build a chain with the default 6-rung config but injected adapters
    so we can test transitions without real network calls."""
    default_keys = {
        "anthropic": "k_a",
        "openrouter": "k_o",
        "minimax": "k_m",
        "deepseek": "k_d",
        "gemini": "k_g",
    }
    chain = LLMChain(
        chain=[
            ("anthropic", "claude-opus-4-7"),
            ("openrouter", "openai/gpt-5"),
            ("anthropic", "claude-sonnet-4-6"),
            ("minimax", "MiniMax-M2"),
            ("deepseek", "deepseek-chat"),
            ("gemini", "gemini-2.5-pro"),
        ],
        keys=keys if keys is not None else default_keys,
    )
    # Monkey-patch the adapter map.
    from fleet.llm import provider_chain as pc

    pc._ADAPTERS = adapters
    return chain


@pytest.mark.asyncio
async def test_first_rung_success() -> None:
    """Opus answers on first try — no fallback fires."""
    adapter_anthropic = AsyncMock(return_value="ok answer")
    chain = _make_chain(
        {
            "anthropic": adapter_anthropic,
            "openrouter": AsyncMock(return_value="WRONG"),
            "minimax": AsyncMock(return_value="WRONG"),
            "deepseek": AsyncMock(return_value="WRONG"),
            "gemini": AsyncMock(return_value="WRONG"),
        }
    )
    result = await chain.complete("hello")
    assert result.text == "ok answer"
    assert result.model_used == "anthropic/claude-opus-4-7"
    assert len(result.rungs_attempted) == 1
    assert result.rungs_attempted[0].outcome == "ok"


@pytest.mark.asyncio
async def test_fallback_through_3_rungs_on_rate_limit() -> None:
    """Opus + GPT + Sonnet all 429 → MiniMax answers."""
    adapter_anthropic = AsyncMock(side_effect=ProviderTransientError("429"))
    adapter_openrouter = AsyncMock(side_effect=ProviderTransientError("429"))
    adapter_minimax = AsyncMock(return_value="from minimax")
    chain = _make_chain(
        {
            "anthropic": adapter_anthropic,
            "openrouter": adapter_openrouter,
            "minimax": adapter_minimax,
            "deepseek": AsyncMock(),
            "gemini": AsyncMock(),
        }
    )
    # Shorten backoff for test speed
    chain.PER_RUNG_ATTEMPTS = 1
    result = await chain.complete("hello")
    assert result.text == "from minimax"
    assert result.model_used == "minimax/MiniMax-M2"
    # 3 rungs attempted before success: opus, gpt, sonnet (transient),
    # then minimax (ok).
    assert len(result.rungs_attempted) == 4
    assert result.rungs_attempted[-1].outcome == "ok"


@pytest.mark.asyncio
async def test_missing_key_skips_rung() -> None:
    """Rung skipped when key empty; chain moves to next without calling adapter."""
    adapter_anthropic = AsyncMock(return_value="WRONG")
    adapter_openrouter = AsyncMock(return_value="from openrouter")
    chain = _make_chain(
        {
            "anthropic": adapter_anthropic,
            "openrouter": adapter_openrouter,
            "minimax": AsyncMock(),
            "deepseek": AsyncMock(),
            "gemini": AsyncMock(),
        },
        keys={
            "anthropic": "",  # missing
            "openrouter": "k",
            "minimax": "k",
            "deepseek": "k",
            "gemini": "k",
        },
    )
    result = await chain.complete("hello")
    assert result.text == "from openrouter"
    assert result.rungs_attempted[0].outcome == "missing_key"
    # The anthropic adapter must NOT have been invoked.
    adapter_anthropic.assert_not_called()


@pytest.mark.asyncio
async def test_auth_error_falls_through_no_retry() -> None:
    """401 from a rung → don't retry that rung, jump to next immediately."""
    adapter_anthropic = AsyncMock(side_effect=ProviderAuthError("401"))
    adapter_openrouter = AsyncMock(return_value="ok")
    chain = _make_chain(
        {
            "anthropic": adapter_anthropic,
            "openrouter": adapter_openrouter,
            "minimax": AsyncMock(),
            "deepseek": AsyncMock(),
            "gemini": AsyncMock(),
        }
    )
    result = await chain.complete("hello")
    assert result.text == "ok"
    # Auth raises → ONE call (not PER_RUNG_ATTEMPTS) before falling through.
    assert adapter_anthropic.call_count == 1


@pytest.mark.asyncio
async def test_permanent_error_aborts_chain() -> None:
    """400 invalid input → don't try further rungs."""
    adapter_anthropic = AsyncMock(side_effect=ProviderPermanentError("400 bad input"))
    adapter_openrouter = AsyncMock(return_value="should not be called")
    chain = _make_chain(
        {
            "anthropic": adapter_anthropic,
            "openrouter": adapter_openrouter,
            "minimax": AsyncMock(),
            "deepseek": AsyncMock(),
            "gemini": AsyncMock(),
        }
    )
    with pytest.raises(LLMChainExhaustedError) as exc_info:
        await chain.complete("hello")
    assert any(r.outcome == "permanent" for r in exc_info.value.rungs)
    # Later rungs MUST NOT be attempted on permanent error.
    adapter_openrouter.assert_not_called()


@pytest.mark.asyncio
async def test_all_rungs_fail_raises_exhausted() -> None:
    """Every rung transient-fails → LLMChainExhaustedError with full diagnostic."""
    chain = _make_chain(
        {
            "anthropic": AsyncMock(side_effect=ProviderTransientError("429")),
            "openrouter": AsyncMock(side_effect=ProviderTransientError("429")),
            "minimax": AsyncMock(side_effect=ProviderTransientError("429")),
            "deepseek": AsyncMock(side_effect=ProviderTransientError("429")),
            "gemini": AsyncMock(side_effect=ProviderTransientError("429")),
        }
    )
    chain.PER_RUNG_ATTEMPTS = 1
    with pytest.raises(LLMChainExhaustedError) as exc_info:
        await chain.complete("hello")
    # 4 rungs attempted (anthropic twice since opus + sonnet, plus
    # openrouter, minimax, deepseek, gemini = 6 total)
    assert len(exc_info.value.rungs) == 6
    assert all(r.outcome == "transient" for r in exc_info.value.rungs)


@pytest.mark.asyncio
async def test_prefer_model_starts_at_specific_rung() -> None:
    """prefer_model='sonnet' skips opus+gpt."""
    adapter_anthropic = AsyncMock(return_value="from sonnet")
    chain = _make_chain(
        {
            "anthropic": adapter_anthropic,
            "openrouter": AsyncMock(),
            "minimax": AsyncMock(),
            "deepseek": AsyncMock(),
            "gemini": AsyncMock(),
        }
    )
    result = await chain.complete(
        "hello",
        prefer_model="anthropic/claude-sonnet-4-6",
    )
    assert result.model_used == "anthropic/claude-sonnet-4-6"
    # Only 1 rung attempted because we started at sonnet (rung 3).
    assert len(result.rungs_attempted) == 1
