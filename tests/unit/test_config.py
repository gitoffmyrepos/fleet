import os

import pytest

from fleet.config import Settings


@pytest.fixture(autouse=True)
def _clear_fleet_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("FLEET_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults_when_no_env() -> None:
    s = Settings(_env_file=None)
    assert s.bearer_token == ""
    assert s.graphiti_url == "http://192.168.119.117:30800/mcp"
    assert s.router_model == "claude-sonnet-4-6"
    assert s.per_task_budget_tokens == 200_000
    assert s.registry_refresh_seconds == 300
    assert s.cache_ttl_seconds == 86_400
    assert s.dispatch_timeout_seconds == 1_800


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_BEARER_TOKEN", "test-token")
    monkeypatch.setenv("FLEET_PER_TASK_BUDGET_TOKENS", "50000")
    s = Settings(_env_file=None)
    assert s.bearer_token == "test-token"
    assert s.per_task_budget_tokens == 50_000


def test_dry_run_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FLEET_DRY_RUN", "true")
    s = Settings(_env_file=None)
    assert s.dry_run is True


def test_load_returns_settings_instance() -> None:
    from fleet.config import load

    s = load()
    assert isinstance(s, Settings)
