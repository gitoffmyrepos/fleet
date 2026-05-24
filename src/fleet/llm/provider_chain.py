"""SP-F (2026-05-24) — work-LLM provider chain with priority fallback.

Tries a configured list of (provider, model) tuples in order. Falls
through to the next rung on rate-limit / 5xx / timeout / auth errors.
Aborts the chain on 400 (permanent input error).

Default chain (per operator decision 2026-05-24):
    1. anthropic  / claude-opus-4-7         (direct, key wired)
    2. openrouter / openai/gpt-5            (via openrouter, key wired)
    3. anthropic  / claude-sonnet-4-6       (direct, key wired)
    4. minimax    / minimax-m2              (direct, key wired)
    5. deepseek   / deepseek-chat           (direct, key wired)
    6. gemini     / gemini-2.5-pro          (direct, last-resort)

The chain is DISTINCT from `fleet.router.Router` which classifies tasks
into dispatch kinds (swarm/phase/subagent/...). This module provides
the actual completion API used by SP-E agents (Openclaw + Hermes).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from fleet.config import Settings
from fleet.llm.providers import (
    ProviderAuthError,
    ProviderError,
    ProviderPermanentError,
    ProviderTransientError,
)
from fleet.llm.providers import anthropic as _anthropic
from fleet.llm.providers import deepseek as _deepseek
from fleet.llm.providers import gemini as _gemini
from fleet.llm.providers import minimax as _minimax
from fleet.llm.providers import openrouter as _openrouter
from fleet.telemetry import Telemetry

# Adapter signature: complete(prompt, model, max_tokens, system, api_key, timeout_s) -> str
_AdapterFn = Callable[..., Awaitable[str]]

_ADAPTERS: dict[str, _AdapterFn] = {
    "anthropic": _anthropic.complete,
    "openrouter": _openrouter.complete,
    "minimax": _minimax.complete,
    "deepseek": _deepseek.complete,
    "gemini": _gemini.complete,
}


DEFAULT_CHAIN: list[tuple[str, str]] = [
    ("anthropic", "claude-opus-4-7"),
    ("openrouter", "openai/gpt-5"),
    ("anthropic", "claude-sonnet-4-6"),
    ("minimax", "MiniMax-M2"),
    ("deepseek", "deepseek-chat"),
    ("gemini", "gemini-2.5-pro"),
]


@dataclass
class RungAttempt:
    provider: str
    model: str
    outcome: str  # "ok" | "transient" | "auth" | "permanent" | "missing_key"
    elapsed_ms: int
    error: str | None = None


@dataclass
class LLMResult:
    text: str
    model_used: str  # "anthropic/claude-opus-4-7" form
    rungs_attempted: list[RungAttempt] = field(default_factory=list)
    elapsed_ms: int = 0


class LLMChainExhaustedError(Exception):
    """All rungs failed or were skipped. Includes per-rung diagnostics."""

    def __init__(self, rungs: list[RungAttempt]) -> None:
        self.rungs = rungs
        summary = ", ".join(f"{r.provider}/{r.model}={r.outcome}" for r in rungs)
        super().__init__(f"LLM chain exhausted: {summary}")


class LLMChain:
    """Async work-LLM router with priority fallback."""

    # Per-rung retry attempts on transient errors before falling through
    PER_RUNG_ATTEMPTS = 3
    # Backoff between attempts (exponential: 1s, 2s)
    BACKOFF_SECONDS = (1.0, 2.0)
    # Per-call timeout (each adapter)
    DEFAULT_TIMEOUT_S = 60.0
    # Total chain wall-clock cap
    MAX_CHAIN_WALL_S = 300.0

    def __init__(
        self,
        *,
        chain: list[tuple[str, str]],
        keys: dict[str, str],
        telemetry: Telemetry | None = None,
    ) -> None:
        self._chain = chain
        self._keys = keys
        self._t = telemetry

    @classmethod
    def from_settings(cls, settings: Settings, *, telemetry: Telemetry | None = None) -> LLMChain:
        keys = {
            "anthropic": settings.anthropic_api_key,
            "openrouter": settings.openrouter_api_key,
            "minimax": settings.minimax_api_key,
            "deepseek": settings.deepseek_api_key,
            "gemini": settings.gemini_api_key,
        }
        return cls(chain=DEFAULT_CHAIN, keys=keys, telemetry=telemetry)

    async def complete(
        self,
        prompt: str,
        *,
        max_tokens: int = 4000,
        system: str | None = None,
        prefer_model: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        task_id: str | None = None,
    ) -> LLMResult:
        """Try rungs in order, return first successful completion.

        prefer_model: optional "provider/model" string to start FROM that
        rung (e.g. "anthropic/claude-sonnet-4-6" skips rungs 1-2).
        Useful when a caller has a strong preference but still wants
        fallback if their preferred model is down.

        Raises LLMChainExhaustedError if all rungs fail.
        """
        start_chain = time.monotonic()
        rungs: list[RungAttempt] = []

        # Compute the starting index based on prefer_model.
        start_idx = 0
        if prefer_model:
            for i, (p, m) in enumerate(self._chain):
                if f"{p}/{m}" == prefer_model or m == prefer_model:
                    start_idx = i
                    break

        for provider, model in self._chain[start_idx:]:
            # Honor total wall-clock cap.
            if time.monotonic() - start_chain > self.MAX_CHAIN_WALL_S:
                rungs.append(
                    RungAttempt(
                        provider=provider,
                        model=model,
                        outcome="wall_cap",
                        elapsed_ms=0,
                        error=f"chain wall-clock cap {self.MAX_CHAIN_WALL_S}s exceeded",
                    )
                )
                break

            api_key = self._keys.get(provider, "")
            if not api_key:
                attempt = RungAttempt(
                    provider=provider,
                    model=model,
                    outcome="missing_key",
                    elapsed_ms=0,
                    error=f"no api_key for {provider}",
                )
                rungs.append(attempt)
                await self._fire_event(task_id, provider, model, attempt)
                continue

            adapter = _ADAPTERS[provider]
            text, attempt = await self._try_rung(
                adapter=adapter,
                provider=provider,
                model=model,
                prompt=prompt,
                max_tokens=max_tokens,
                system=system,
                api_key=api_key,
                timeout_s=timeout_s,
            )
            rungs.append(attempt)
            await self._fire_event(task_id, provider, model, attempt)

            if attempt.outcome == "ok":
                return LLMResult(
                    text=text or "",
                    model_used=f"{provider}/{model}",
                    rungs_attempted=rungs,
                    elapsed_ms=int((time.monotonic() - start_chain) * 1000),
                )
            if attempt.outcome == "permanent":
                # Don't try further rungs — input is invalid.
                break

        raise LLMChainExhaustedError(rungs)

    async def _try_rung(
        self,
        *,
        adapter: _AdapterFn,
        provider: str,
        model: str,
        prompt: str,
        max_tokens: int,
        system: str | None,
        api_key: str,
        timeout_s: float,
    ) -> tuple[str | None, RungAttempt]:
        """Run a single rung with up to PER_RUNG_ATTEMPTS retries on
        transient errors. Returns (text|None, RungAttempt)."""
        start = time.monotonic()
        last_error: str | None = None

        for attempt_i in range(self.PER_RUNG_ATTEMPTS):
            try:
                text = await adapter(
                    prompt,
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    api_key=api_key,
                    timeout_s=timeout_s,
                )
                return text, RungAttempt(
                    provider=provider,
                    model=model,
                    outcome="ok",
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                )
            except ProviderAuthError as e:
                # Auth → don't retry this rung, fall through immediately.
                return None, RungAttempt(
                    provider=provider,
                    model=model,
                    outcome="auth",
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    error=str(e)[:200],
                )
            except ProviderPermanentError as e:
                # 4xx other than auth/rate-limit → abort the chain.
                return None, RungAttempt(
                    provider=provider,
                    model=model,
                    outcome="permanent",
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    error=str(e)[:200],
                )
            except ProviderTransientError as e:
                last_error = str(e)[:200]
                if attempt_i + 1 < self.PER_RUNG_ATTEMPTS:
                    await asyncio.sleep(
                        self.BACKOFF_SECONDS[min(attempt_i, len(self.BACKOFF_SECONDS) - 1)]
                    )
                continue
            except ProviderError as e:
                last_error = str(e)[:200]
                break
            except Exception as e:  # safety net for unmapped adapter errors
                last_error = f"unexpected: {type(e).__name__}: {e}"[:200]
                break

        return None, RungAttempt(
            provider=provider,
            model=model,
            outcome="transient",
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error=last_error,
        )

    async def _fire_event(
        self,
        task_id: str | None,
        provider: str,
        model: str,
        attempt: RungAttempt,
    ) -> None:
        if self._t is None:
            return
        await self._t.event(
            task_id=task_id or "no-task",
            kind="fleet_llm_attempt",
            body={
                "provider": provider,
                "model": model,
                "outcome": attempt.outcome,
                "elapsed_ms": attempt.elapsed_ms,
                "error": attempt.error,
            },
        )
