"""OpenRouter adapter — proxies OpenAI, MiniMax, and many others.

Used in the chain to reach `openai/gpt-5` since we route GPT through
OpenRouter (operator decision 2026-05-24).
"""

from __future__ import annotations

from fleet.llm.providers.openai_compat import complete_openai_compat

BASE_URL = "https://openrouter.ai/api/v1"


async def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    api_key: str,
    timeout_s: float,
) -> str:
    # OpenRouter requires HTTP-Referer + X-Title headers for billing
    # attribution and to avoid generic-client throttling.
    extra_headers = {
        "HTTP-Referer": "https://github.com/gitoffmyrepos/fleet",
        "X-Title": "Fleet MCP (homelab)",
    }
    return await complete_openai_compat(
        prompt,
        model=model,
        max_tokens=max_tokens,
        system=system,
        api_key=api_key,
        base_url=BASE_URL,
        timeout_s=timeout_s,
        extra_headers=extra_headers,
    )
