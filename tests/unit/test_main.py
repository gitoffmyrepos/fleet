"""Tests for the __main__ bootstrap helpers."""

from unittest.mock import AsyncMock, patch

import pytest

from fleet.__main__ import _build_deps
from fleet.config import Settings


@pytest.mark.asyncio
async def test_build_deps_constructs_all_backends() -> None:
    s = Settings(
        anthropic_api_key="",
        graphiti_url="http://fake/mcp",
        bearer_token="",
        _env_file=None,  # type: ignore[call-arg]
    )
    with patch("fleet.__main__.Registry") as mock_registry_cls:
        instance = mock_registry_cls.return_value
        instance.load = AsyncMock(return_value=None)
        deps = await _build_deps(s)
    assert deps.graphiti is not None
    assert deps.telemetry is not None
    assert deps.cache is not None
    assert deps.circuits is not None
    assert deps.router is not None
    assert deps.swarm is not None
    assert deps.phase is not None
    assert deps.subagent is not None
    assert deps.verify is not None


@pytest.mark.asyncio
async def test_build_deps_skips_anthropic_when_no_api_key() -> None:
    s = Settings(anthropic_api_key="", _env_file=None)  # type: ignore[call-arg]
    with patch("fleet.__main__.Registry") as mock_registry_cls:
        mock_registry_cls.return_value.load = AsyncMock(return_value=None)
        deps = await _build_deps(s)
    assert deps.router._a is None
