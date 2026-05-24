"""Gemini adapter — Google Generative AI API.

Gemini has a different API shape from OpenAI-compatible providers (no
/chat/completions endpoint, separate SDK). We call the REST endpoint
directly via httpx to avoid the heavyweight `google-generativeai` SDK
dependency.

Docs: https://ai.google.dev/api/generate-content
Endpoint: POST /v1beta/models/{model}:generateContent?key={api_key}
"""

from __future__ import annotations

import asyncio

import httpx

from fleet.llm.providers import (
    ProviderAuthError,
    ProviderPermanentError,
    ProviderTransientError,
)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"


async def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    api_key: str,
    timeout_s: float,
) -> str:
    if not api_key:
        raise ProviderAuthError("gemini api_key missing")

    # Gemini wants `models/<id>` in the path; accept either form.
    model_path = model if model.startswith("models/") else f"models/{model}"
    url = f"{BASE_URL}/{model_path}:generateContent"

    body: dict = {
        "contents": [
            {"role": "user", "parts": [{"text": prompt}]},
        ],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
        },
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key,
    }

    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await asyncio.wait_for(
                client.post(url, json=body, headers=headers),
                timeout=timeout_s,
            )
    except TimeoutError as e:
        raise ProviderTransientError(f"gemini timeout after {timeout_s}s") from e
    except httpx.ConnectError as e:
        raise ProviderTransientError(f"gemini conn: {e}") from e
    except httpx.HTTPError as e:
        raise ProviderTransientError(f"gemini http: {e}") from e

    status = resp.status_code
    if status == 401 or status == 403:
        raise ProviderAuthError(f"gemini auth: {status}")
    if status == 429 or status >= 500:
        raise ProviderTransientError(f"gemini status {status}: {resp.text[:200]}")
    if status >= 400:
        raise ProviderPermanentError(f"gemini status {status}: {resp.text[:200]}")

    try:
        data = resp.json()
        # Response shape: candidates[0].content.parts[*].text
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (KeyError, IndexError, ValueError) as e:
        raise ProviderPermanentError(f"gemini bad response shape: {e}") from e
