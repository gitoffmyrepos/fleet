"""Environment-driven configuration.

The path defaults below (`ruflo_cli_path`, `claude_cli_path`, `*_root`) target
host-mode operation on kelvin's homelab. Containerised deployments MUST
override them via `FLEET_*` env vars (see `deploy/helm/values.yaml`, added in
Phase 14).

TODO(Phase 14 hardening): convert `bearer_token`, `graphiti_bearer`, and
`anthropic_api_key` to `SecretStr` so they don't leak through repr/json/log
serialisation. Deferred to the Helm/Vault wiring task because the change
cascades through every downstream caller (`get_secret_value()`).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FLEET_", env_file=".env", extra="ignore")

    bearer_token: str = ""
    graphiti_url: str = "http://192.168.119.117:30800/mcp"
    graphiti_bearer: str = ""

    router_model: str = "claude-sonnet-4-6"
    router_confidence_threshold: float = 0.7
    router_safe_fallback_threshold: float = 0.5

    anthropic_api_key: str = ""

    per_task_budget_tokens: int = 200_000
    registry_refresh_seconds: int = 300
    cache_ttl_seconds: int = 86_400
    dispatch_timeout_seconds: int = 1_800

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
    return Settings()
