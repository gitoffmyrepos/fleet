"""Unit tests for the non-OpenAI-shaped adapters and the local rung.

Gemini speaks its own REST shape, Anthropic goes through its SDK, and the
local rung is a thin base_url-bound wrapper over the shared adapter. All
three are patched at the module level — nothing here touches the network.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from fleet.llm.providers import (
    ProviderAuthError,
    ProviderPermanentError,
    ProviderTransientError,
)
from fleet.llm.providers import anthropic as anthropic_adapter
from fleet.llm.providers import gemini as gemini_adapter
from fleet.llm.providers import local as local_adapter

# ─── gemini ────────────────────────────────────────────────────────────────


def _response(status: int, payload: Any = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


def _patch_gemini_post(result: Any) -> Any:
    client = MagicMock()
    if isinstance(result, Exception):
        client.post = AsyncMock(side_effect=result)
    else:
        client.post = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("fleet.llm.providers.gemini.httpx.AsyncClient", return_value=ctx), client


async def _gemini(**kw: Any) -> str:
    defaults = dict(model="gemini-2.5-pro", max_tokens=8, system=None, api_key="k", timeout_s=5.0)
    defaults.update(kw)
    return await gemini_adapter.complete("hi", **defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_gemini_missing_key_is_auth_error() -> None:
    with pytest.raises(ProviderAuthError):
        await _gemini(api_key="")


@pytest.mark.asyncio
async def test_gemini_joins_all_text_parts_and_builds_model_path() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": "a"}, {"text": "b"}]}}]}
    patcher, client = _patch_gemini_post(_response(200, payload))
    with patcher:
        assert await _gemini() == "ab"
    assert client.post.call_args[0][0].endswith("/models/gemini-2.5-pro:generateContent")


@pytest.mark.asyncio
async def test_gemini_accepts_an_already_qualified_model_path() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
    patcher, client = _patch_gemini_post(_response(200, payload))
    with patcher:
        await _gemini(model="models/gemini-2.5-pro")
    url = client.post.call_args[0][0]
    assert "models/models/" not in url


@pytest.mark.asyncio
async def test_gemini_system_prompt_becomes_system_instruction() -> None:
    payload = {"candidates": [{"content": {"parts": [{"text": "x"}]}}]}
    patcher, client = _patch_gemini_post(_response(200, payload))
    with patcher:
        await _gemini(system="be terse")
    body = client.post.call_args[1]["json"]
    assert body["systemInstruction"] == {"parts": [{"text": "be terse"}]}
    assert body["generationConfig"]["maxOutputTokens"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderTransientError),
        (500, ProviderTransientError),
        (400, ProviderPermanentError),
    ],
)
async def test_gemini_status_mapping(status: int, expected: type[Exception]) -> None:
    patcher, _ = _patch_gemini_post(_response(status, {}, text="err"))
    with patcher, pytest.raises(expected):
        await _gemini()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc", [TimeoutError("slow"), httpx.ConnectError("refused"), httpx.HTTPError("boom")]
)
async def test_gemini_network_failures_are_transient(exc: Exception) -> None:
    patcher, _ = _patch_gemini_post(exc)
    with patcher, pytest.raises(ProviderTransientError):
        await _gemini()


@pytest.mark.asyncio
async def test_gemini_bad_shape_is_permanent() -> None:
    patcher, _ = _patch_gemini_post(_response(200, {"candidates": []}))
    with patcher, pytest.raises(ProviderPermanentError):
        await _gemini()


# ─── anthropic ─────────────────────────────────────────────────────────────


def _patch_anthropic(result: Any) -> Any:
    create = (
        AsyncMock(side_effect=result)
        if isinstance(result, Exception)
        else AsyncMock(return_value=result)
    )
    client = MagicMock()
    client.messages.create = create
    return patch.object(anthropic_adapter._sdk, "AsyncAnthropic", return_value=client), create


def _msg(*texts: str) -> MagicMock:
    msg = MagicMock()
    msg.content = [MagicMock(text=t) for t in texts]
    return msg


async def _anthropic(**kw: Any) -> str:
    defaults = dict(model="claude-x", max_tokens=8, system=None, api_key="k", timeout_s=5.0)
    defaults.update(kw)
    return await anthropic_adapter.complete("hi", **defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_anthropic_missing_key_is_auth_error() -> None:
    with pytest.raises(ProviderAuthError):
        await _anthropic(api_key="")


@pytest.mark.asyncio
async def test_anthropic_concatenates_text_blocks() -> None:
    patcher, create = _patch_anthropic(_msg("foo", "bar"))
    with patcher:
        assert await _anthropic() == "foobar"
    assert create.call_args[1]["model"] == "claude-x"


@pytest.mark.asyncio
async def test_anthropic_omits_system_when_absent() -> None:
    patcher, create = _patch_anthropic(_msg("x"))
    with patcher:
        await _anthropic()
    assert create.call_args[1]["system"] is anthropic_adapter._sdk.omit


@pytest.mark.asyncio
async def test_anthropic_passes_system_when_present() -> None:
    patcher, create = _patch_anthropic(_msg("x"))
    with patcher:
        await _anthropic(system="be terse")
    assert create.call_args[1]["system"] == "be terse"


@pytest.mark.asyncio
async def test_anthropic_timeout_is_transient() -> None:
    patcher, _ = _patch_anthropic(TimeoutError("slow"))
    with patcher, pytest.raises(ProviderTransientError):
        await _anthropic()


# ─── local rung ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_local_uses_explicit_base_url_when_bound() -> None:
    shared = AsyncMock(return_value="ok")
    with patch("fleet.llm.providers.local.complete_openai_compat", shared):
        out = await local_adapter.complete(
            "hi",
            model="qwen3.8-27b",
            max_tokens=8,
            system=None,
            api_key="k",
            timeout_s=5.0,
            base_url="http://127.0.0.1:9/v1",
        )
    assert out == "ok"
    assert shared.call_args[1]["base_url"] == "http://127.0.0.1:9/v1"


@pytest.mark.asyncio
async def test_local_falls_back_to_settings_base_url() -> None:
    shared = AsyncMock(return_value="ok")
    with (
        patch("fleet.llm.providers.local.complete_openai_compat", shared),
        patch("fleet.llm.providers.local._resolve_base_url", return_value="http://resolved/v1"),
    ):
        await local_adapter.complete(
            "hi", model="m", max_tokens=8, system=None, api_key="k", timeout_s=5.0
        )
    assert shared.call_args[1]["base_url"] == "http://resolved/v1"


def test_local_resolve_base_url_defaults_when_settings_unreadable() -> None:
    with patch("fleet.llm.providers.local.Settings", side_effect=RuntimeError("no env")):
        assert local_adapter._resolve_base_url() == local_adapter.DEFAULT_BASE_URL
