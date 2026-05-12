"""Fleet entrypoint: launch HTTP MCP server."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import structlog
import uvicorn
from anthropic import AsyncAnthropic

from .cache import Cache
from .circuit import CircuitRegistry
from .config import Settings, load
from .dispatcher.phase import PhaseDispatcher
from .dispatcher.subagent import SubagentDispatcher
from .dispatcher.swarm import SwarmDispatcher
from .dispatcher.verify import VerifyDispatcher
from .graphiti_client import GraphitiClient
from .registry import Registry, RegistryConfig
from .router import Router
from .server import build_app
from .telemetry import Telemetry


@dataclass
class _Deps:
    """Runtime container for all wired backends."""

    graphiti: GraphitiClient
    telemetry: Telemetry
    cache: Cache
    circuits: CircuitRegistry
    registry: Registry
    router: Router
    swarm: SwarmDispatcher
    phase: PhaseDispatcher
    subagent: SubagentDispatcher
    verify: VerifyDispatcher


async def _build_deps(settings: Settings) -> _Deps:
    graphiti = GraphitiClient(url=settings.graphiti_url, bearer=settings.graphiti_bearer)
    telemetry = Telemetry(graphiti=graphiti)
    cache = Cache(
        graphiti=graphiti,
        telemetry=telemetry,
        ttl_seconds=settings.cache_ttl_seconds,
    )
    circuits = CircuitRegistry(
        failure_threshold=settings.circuit_failure_threshold,
        window_seconds=settings.circuit_window_seconds,
        cooldown_seconds=settings.circuit_cooldown_seconds,
    )
    # Pre-instantiate the known upstreams so operators can `circuit_close` them
    # before any dispatch has registered the breaker lazily.
    for upstream in ("ruflo", "superpowers", "gsd"):
        circuits.get(upstream)
    rcfg = RegistryConfig(
        sources=[
            {"name": "ruflo", "root": settings.ruflo_agents_root, "pattern": "*.md"},
            {
                "name": "superpowers",
                "root": f"{settings.skills_root}/superpowers",
                "pattern": "*/SKILL.md",
            },
            {"name": "claude", "root": settings.agents_root, "pattern": "*.md"},
            {"name": "gsd", "root": f"{settings.commands_root}/gsd", "pattern": "*.md"},
        ]
    )
    registry = Registry(rcfg, graphiti=graphiti)
    await registry.load()
    anthropic: Any = (
        AsyncAnthropic(api_key=settings.anthropic_api_key) if settings.anthropic_api_key else None
    )
    router = Router(settings=settings, anthropic=anthropic, telemetry=telemetry)
    swarm = SwarmDispatcher(
        circuits=circuits,
        telemetry=telemetry,
        timeout_seconds=settings.dispatch_timeout_seconds,
        cli_path=settings.ruflo_cli_path,
        workdir=settings.ruflo_workdir,
    )
    phase = PhaseDispatcher(
        circuits=circuits,
        telemetry=telemetry,
        timeout_seconds=settings.dispatch_timeout_seconds,
        claude_path=settings.claude_cli_path,
    )
    subagent = SubagentDispatcher(
        circuits=circuits,
        telemetry=telemetry,
        timeout_seconds=settings.dispatch_timeout_seconds,
        claude_path=settings.claude_cli_path,
    )
    verify = VerifyDispatcher(
        circuits=circuits,
        telemetry=telemetry,
        timeout_seconds=settings.dispatch_timeout_seconds,
        claude_path=settings.claude_cli_path,
    )
    return _Deps(
        graphiti=graphiti,
        telemetry=telemetry,
        cache=cache,
        circuits=circuits,
        registry=registry,
        router=router,
        swarm=swarm,
        phase=phase,
        subagent=subagent,
        verify=verify,
    )


def _audit_client_tokens(active_token: str, log: logging.Logger) -> None:
    """2026-05-12: scan known client config locations at startup. Log a
    WARNING for each that holds a token DIFFERENT from the active one
    in proximity to a Fleet URL — a stale entry will silently 401 every
    dispatch from that client and the only signal today is when someone
    tries to use it.

    Proximity filter: only `Bearer <token>` references that share a line
    (or short window) with a Fleet URL fragment count. Pure `Bearer ...`
    references elsewhere in the file — GitHub PATs in permission
    allow-lists, Anthropic keys, Vault tokens — are ignored.

    Inert by design: only emits log lines. Never fails startup. The
    rotation script `fleet-rotate-token` does the actual rewriting.
    """
    if not active_token:
        return
    import os
    import re

    # Mirror of fleet-rotate-token's CLIENT_FILES registry. Keep in sync
    # with ~/.local/bin/fleet-rotate-token. Both lists answer the same
    # question: "where is Fleet's bearer token configured?"
    home = os.path.expanduser("~")
    client_paths = [
        f"{home}/.claude.json",
        f"{home}/.claude/settings.local.json",
        f"{home}/.hermes/config.yaml",
        f"{home}/.openclaw/openclaw.json",
        f"{home}/.config/goose/config.yaml",
    ]
    # Fleet URL fragments that anchor "this Bearer is Fleet's, not some
    # other API's". The fleet.strategybase.io route lands here when the
    # k8s cluster migration completes.
    fleet_url_re = re.compile(
        r"127\.0\.0\.1:18001|localhost:18001|fleet\.strategybase\.io|fleet-mcp",
        re.IGNORECASE,
    )
    token_re = re.compile(r"Bearer\s+([A-Za-z0-9._\-]{8,})")
    stale_count = 0
    for path in client_paths:
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
        except OSError:
            continue
        # Walk line-by-line. A Bearer is Fleet-bound iff:
        #   (a) the same line contains a Fleet URL fragment, OR
        #   (b) one of the 3 lines before/after contains one
        # That covers both inline-JSON (Bearer + url on one line) and
        # the multi-line YAML/JSON-with-object-formatting case.
        lines = content.splitlines()
        fleet_token_lines = []
        for i, line in enumerate(lines):
            for tok in token_re.findall(line):
                if tok == active_token:
                    continue
                # Window around the line for the proximity check.
                lo, hi = max(0, i - 3), min(len(lines), i + 4)
                window = "\n".join(lines[lo:hi])
                if fleet_url_re.search(window):
                    fleet_token_lines.append((i + 1, tok))
        if fleet_token_lines:
            stale_count += 1
            preview = ", ".join(f"L{ln}:{tok[:8]}..." for ln, tok in fleet_token_lines[:5])
            log.warning(
                "client config %s holds %d Fleet-bound stale token(s) [%s] "
                "— those clients will 401 on dispatch. Run "
                "`fleet-rotate-token --check` to inspect.",
                path,
                len(fleet_token_lines),
                preview,
            )
    if stale_count == 0:
        log.info(
            "client-token audit: all %d known configs match the active token",
            len(client_paths),
        )
    else:
        log.warning(
            "client-token audit: %d/%d configs have stale tokens",
            stale_count,
            len(client_paths),
        )


def main() -> None:
    settings = load()
    logging.basicConfig(level=settings.log_level)
    structlog.configure()
    log = logging.getLogger("fleet")
    # Startup health check: warn loudly if any known client config holds
    # a stale token. Prevents the 2026-05-11 incident from recurring
    # silently. The rotation helper script (`fleet-rotate-token`) does
    # the actual fixing; this only surfaces the problem.
    _audit_client_tokens(settings.bearer_token, log)
    if settings.bearer_token_previous:
        log.warning(
            "FLEET_BEARER_TOKEN_PREVIOUS is set — rotation window active. "
            "Both primary and previous tokens accepted; clear "
            "FLEET_BEARER_TOKEN_PREVIOUS once every client is updated."
        )
    deps = asyncio.run(_build_deps(settings))
    app = build_app(
        deps=deps,
        bearer_token=settings.bearer_token,
        bearer_token_previous=settings.bearer_token_previous,
    )
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
