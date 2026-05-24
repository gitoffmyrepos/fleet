"""DeepSeek adapter — direct DeepSeek API (OpenAI-compatible shape)."""

from __future__ import annotations

from fleet.llm.providers.openai_compat import complete_openai_compat

BASE_URL = "https://api.deepseek.com/v1"


async def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    api_key: str,
    timeout_s: float,
) -> str:
    return await complete_openai_compat(
        prompt,
        model=model,
        max_tokens=max_tokens,
        system=system,
        api_key=api_key,
        base_url=BASE_URL,
        timeout_s=timeout_s,
    )
