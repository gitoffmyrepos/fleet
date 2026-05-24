"""OpenAI-compatible client used by openrouter / minimax / deepseek.

All three providers expose the same /chat/completions API shape, so we
share one adapter parameterised by base_url.
"""

from __future__ import annotations

import asyncio

import httpx

from fleet.llm.providers import (
    ProviderAuthError,
    ProviderPermanentError,
    ProviderTransientError,
)


async def complete_openai_compat(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    api_key: str,
    base_url: str,
    timeout_s: float,
    extra_headers: dict[str, str] | None = None,
) -> str:
    """Hit POST {base_url}/chat/completions and return assistant text.

    Returns the content of choices[0].message.content. Raises adapter
    errors mapped from HTTP status / network errors.
    """
    if not api_key:
        raise ProviderAuthError(f"{base_url} api_key missing")

    messages: list[dict[str, str]] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
    }

    url = f"{base_url.rstrip('/')}/chat/completions"

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await asyncio.wait_for(
                client.post(url, json=body, headers=headers),
                timeout=timeout_s,
            )
    except TimeoutError as e:
        raise ProviderTransientError(f"{base_url} timeout after {timeout_s}s") from e
    except httpx.ConnectError as e:
        raise ProviderTransientError(f"{base_url} conn: {e}") from e
    except httpx.HTTPError as e:
        raise ProviderTransientError(f"{base_url} http: {e}") from e

    status = resp.status_code
    if status == 401 or status == 403:
        raise ProviderAuthError(f"{base_url} auth: {status}")
    if status == 429 or status >= 500:
        raise ProviderTransientError(f"{base_url} status {status}: {resp.text[:200]}")
    if status >= 400:
        raise ProviderPermanentError(f"{base_url} status {status}: {resp.text[:200]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError) as e:
        raise ProviderPermanentError(f"{base_url} bad response shape: {e}") from e
