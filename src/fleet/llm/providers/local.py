"""Local-LLM adapter — self-hosted OpenAI-compatible gateway.

Default endpoint is llm.strategybase.io serving qwen3.8-27b. The base URL
comes from Settings.local_llm_base_url (env FLEET_LOCAL_LLM_BASE_URL) so
the same adapter works against any OpenAI-compatible local endpoint.
`LLMChain.from_settings` binds the configured base_url explicitly; a bare
call (e.g. a hand-built LLMChain) resolves it from a fresh Settings read.
"""

from __future__ import annotations

from fleet.config import Settings
from fleet.llm.providers.openai_compat import complete_openai_compat

DEFAULT_BASE_URL = "https://llm.strategybase.io/v1"


def _resolve_base_url() -> str:
    """Base URL from Settings; fall back to the default if unreadable."""
    try:
        return Settings().local_llm_base_url or DEFAULT_BASE_URL
    except Exception:  # pragma: no cover - defensive
        return DEFAULT_BASE_URL


async def complete(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    system: str | None,
    api_key: str,
    timeout_s: float,
    base_url: str | None = None,
) -> str:
    if base_url is None:
        base_url = _resolve_base_url()
    return await complete_openai_compat(
        prompt,
        model=model,
        max_tokens=max_tokens,
        system=system,
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
    )
