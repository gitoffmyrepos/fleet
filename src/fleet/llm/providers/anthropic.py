"""Anthropic adapter — claude-opus-4-7, claude-sonnet-4-6, etc."""

from __future__ import annotations

import asyncio

import anthropic as _sdk

from fleet.llm.providers import (
    ProviderAuthError,
    ProviderPermanentError,
    ProviderTransientError,
)


async def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    api_key: str,
    timeout_s: float,
) -> str:
    """Single Anthropic completion. Raises adapter errors on failure."""
    if not api_key:
        raise ProviderAuthError("anthropic api_key missing")

    client = _sdk.AsyncAnthropic(api_key=api_key, timeout=timeout_s)
    try:
        msg = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
                system=system if system else _sdk.NOT_GIVEN,
            ),
            timeout=timeout_s,
        )
    except TimeoutError as e:
        raise ProviderTransientError(f"anthropic timeout after {timeout_s}s") from e
    except _sdk.AuthenticationError as e:
        raise ProviderAuthError(f"anthropic auth: {e}") from e
    except _sdk.RateLimitError as e:
        raise ProviderTransientError(f"anthropic rate-limit: {e}") from e
    except _sdk.APIStatusError as e:
        # 5xx → transient; 4xx (other than auth/rate-limit) → permanent
        status = getattr(e, "status_code", 0)
        if status >= 500:
            raise ProviderTransientError(f"anthropic {status}: {e}") from e
        raise ProviderPermanentError(f"anthropic {status}: {e}") from e
    except _sdk.APIConnectionError as e:
        raise ProviderTransientError(f"anthropic conn: {e}") from e

    # Extract text from all text blocks (skip tool-use blocks if any).
    return "".join(getattr(b, "text", "") for b in msg.content)
