"""Environment-driven configuration.

The path defaults below (`ruflo_cli_path`, `claude_cli_path`, `*_root`) target
host-mode operation on kelvin's homelab. Containerised deployments MUST
override them via `FLEET_*` env vars (see `deploy/helm/values.yaml`, added in
Phase 14).

TODO(Phase 14 hardening): convert `bearer_token`, `graphiti_bearer`, and
`anthropic_api_key` to `SecretStr` so they don't leak through repr/json/log
serialisation. Deferred to the Helm/Vault wiring task because the change
cascades through every downstream caller (`get_secret_value()`).

2026-05-11 (opt-2): bearer token may be sourced from a file via
`FLEET_BEARER_TOKEN_FILE` so the literal doesn't live in .env (which gets
committed). File contents are read on settings construction with surrounding
whitespace stripped. `FLEET_BEARER_TOKEN` env var takes precedence if both
are set (for emergency overrides).

2026-05-12 (zero-downtime rotation): `FLEET_BEARER_TOKEN_PREVIOUS` is
accepted as a secondary token during a rotation window. The fleet-rotate-
token helper sets this to the OLD value when it writes the new one, restarts
Fleet, then rotates client configs, then restarts Fleet AGAIN with the
previous-token unset. Result: zero 401s during rotation — clients that
haven't been updated yet still authenticate via the previous token until
the script retires it. See `~/.local/bin/fleet-rotate-token`.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_token_file(path: str | None) -> str:
    if not path:
        return ""
    p = Path(path).expanduser()
    if not p.is_file():
        return ""
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEET_", env_file=".env", extra="ignore")

    bearer_token: str = ""
    bearer_token_file: str = ""
    # 2026-05-12: secondary token accepted alongside `bearer_token`.
    # Set by `fleet-rotate-token` during the rotation window to the OLD
    # token so clients still using it don't 401 while their configs are
    # being updated. The script clears this after a successful rotation.
    bearer_token_previous: str = ""
    graphiti_url: str = "http://192.168.119.117:30800/mcp"
    graphiti_bearer: str = ""

    router_model: str = "claude-sonnet-4-6"
    router_confidence_threshold: float = 0.7
    router_safe_fallback_threshold: float = 0.5

    anthropic_api_key: str = ""

    per_task_budget_tokens: int = 300_000  # raised from 200k for deep forensic investigations
    registry_refresh_seconds: int = 300
    cache_ttl_seconds: int = 86_400
    # FIX (hermes 2026-05-12): dispatch timeout raised from 1800s (30min) to
    # 3600s (60min). Hive-mind spawn with 20 agents doing deep forensic
    # analysis across 100+ microservices easily exceeds 30 minutes. The
    # subprocess is killed by the kernel before it can finish, causing
    # Fleet MCP timeout errors. 60min gives enough runway for any realistic
    # investigation task while still being bounded.
    dispatch_timeout_seconds: int = 3_600

    circuit_failure_threshold: int = 3
    circuit_window_seconds: int = 600
    circuit_cooldown_seconds: int = 300

    dry_run: bool = False
    log_level: str = "INFO"

    nodeport: int = 30801
    listen_host: str = "0.0.0.0"  # bind all interfaces; intended for container/k8s use
    listen_port: int = 8000

    ruflo_cli_path: str = "/home/kelvin/.local/bin/claude-flow"
    ruflo_workdir: str = "/home/kelvin/.openclaw/workspace/ruflo"
    claude_cli_path: str = "/home/kelvin/.local/bin/claude"
    skills_root: str = "/home/kelvin/.claude/skills"
    commands_root: str = "/home/kelvin/.claude/commands"
    agents_root: str = "/home/kelvin/.claude/agents"
    ruflo_agents_root: str = "/home/kelvin/.openclaw/workspace/ruflo/.claude/agents"


def load() -> Settings:
    s = Settings()
    # 2026-05-11 (opt-2): resolve the file-based bearer when no inline token
    # is configured. Keeps the literal value out of the .env in git.
    if not s.bearer_token and s.bearer_token_file:
        file_token = _read_token_file(s.bearer_token_file)
        if file_token:
            s = s.model_copy(update={"bearer_token": file_token})
    return s
