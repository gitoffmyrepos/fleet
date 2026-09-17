"""Unit tests for the shared OpenAI-compatible adapter.

openrouter, minimax, deepseek and the local rung all funnel through
`complete_openai_compat`, so its request shape and error mapping are
worth pinning down. httpx is patched at the module level — no test here
opens a socket.
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
from fleet.llm.providers.openai_compat import complete_openai_compat

BASE = "https://example.test/v1"


def _response(status: int, payload: Any = None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


def _patch_post(result: Any) -> Any:
    """Patch httpx.AsyncClient so .post returns (or raises) `result`."""
    client = MagicMock()
    if isinstance(result, Exception):
        client.post = AsyncMock(side_effect=result)
    else:
        client.post = AsyncMock(return_value=result)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=client)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return patch("fleet.llm.providers.openai_compat.httpx.AsyncClient", return_value=ctx), client


async def _call(**kw: Any) -> str:
    defaults = dict(
        model="m1",
        max_tokens=16,
        system=None,
        api_key="k",
        base_url=BASE,
        timeout_s=5.0,
    )
    defaults.update(kw)
    return await complete_openai_compat("hello", **defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_missing_api_key_is_an_auth_error_before_any_request() -> None:
    patcher, client = _patch_post(_response(200, {}))
    with patcher, pytest.raises(ProviderAuthError):
        await _call(api_key="")
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_happy_path_returns_content_and_sends_expected_request() -> None:
    payload = {"choices": [{"message": {"content": "hi there"}}]}
    patcher, client = _patch_post(_response(200, payload))
    with patcher:
        assert await _call() == "hi there"
    _, kwargs = client.post.call_args
    assert kwargs["json"] == {
        "model": "m1",
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 16,
    }
    assert kwargs["headers"]["Authorization"] == "Bearer k"
    assert client.post.call_args[0][0] == f"{BASE}/chat/completions"


@pytest.mark.asyncio
async def test_system_prompt_is_prepended_and_extra_headers_merge() -> None:
    payload = {"choices": [{"message": {"content": "ok"}}]}
    patcher, client = _patch_post(_response(200, payload))
    with patcher:
        assert await _call(system="be terse", extra_headers={"X-Title": "t"}) == "ok"
    _, kwargs = client.post.call_args
    assert kwargs["json"]["messages"][0] == {"role": "system", "content": "be terse"}
    assert kwargs["headers"]["X-Title"] == "t"


@pytest.mark.asyncio
async def test_trailing_slash_in_base_url_does_not_double_up() -> None:
    payload = {"choices": [{"message": {"content": "ok"}}]}
    patcher, client = _patch_post(_response(200, payload))
    with patcher:
        await _call(base_url=BASE + "/")
    assert client.post.call_args[0][0] == f"{BASE}/chat/completions"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_statuses_map_to_auth_error(status: int) -> None:
    patcher, _ = _patch_post(_response(status, {}, text="nope"))
    with patcher, pytest.raises(ProviderAuthError):
        await _call()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_rate_limit_and_server_errors_are_transient(status: int) -> None:
    patcher, _ = _patch_post(_response(status, {}, text="busy"))
    with patcher, pytest.raises(ProviderTransientError):
        await _call()


@pytest.mark.asyncio
async def test_other_4xx_is_permanent_so_the_chain_stops() -> None:
    patcher, _ = _patch_post(_response(400, {}, text="bad input"))
    with patcher, pytest.raises(ProviderPermanentError):
        await _call()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("slow"),
        httpx.ConnectError("refused"),
        httpx.HTTPError("boom"),
    ],
)
async def test_network_failures_are_transient(exc: Exception) -> None:
    patcher, _ = _patch_post(exc)
    with patcher, pytest.raises(ProviderTransientError):
        await _call()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},  # no choices key
        {"choices": []},  # empty choices
        {"choices": [{"message": {}}]},  # no content
        {"choices": [{"message": {"content": 42}}]},  # content not a string
    ],
)
async def test_bad_response_shapes_are_permanent(payload: dict) -> None:
    patcher, _ = _patch_post(_response(200, payload))
    with patcher, pytest.raises(ProviderPermanentError):
        await _call()


@pytest.mark.asyncio
async def test_non_json_body_is_permanent() -> None:
    patcher, _ = _patch_post(_response(200, None))
    with patcher, pytest.raises(ProviderPermanentError):
        await _call()
