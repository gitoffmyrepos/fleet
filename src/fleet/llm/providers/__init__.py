"""LLM provider adapters used by `fleet.llm.LLMChain`.

Each adapter exposes one async function:

    async def complete(prompt: str, *, model: str, max_tokens: int,
                       system: str | None, api_key: str,
                       timeout_s: float) -> str

Adapter-specific transient errors are translated into the common
`ProviderTransientError` / `ProviderAuthError` / `ProviderPermanentError`
exceptions defined here so `LLMChain` can decide fallback uniformly.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for adapter-level errors."""


class ProviderTransientError(ProviderError):
    """Rate-limit, 5xx, or timeout — chain should try next rung."""


class ProviderAuthError(ProviderError):
    """401/403 — likely missing/wrong key; chain should try next rung
    (don't waste retries on this rung)."""


class ProviderPermanentError(ProviderError):
    """400/422 invalid input — chain should ABORT (further rungs will
    fail too with same input)."""


__all__ = [
    "ProviderAuthError",
    "ProviderError",
    "ProviderPermanentError",
    "ProviderTransientError",
]
