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
        workdir="/home/kelvin/.openclaw/workspace/ruflo",
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


def main() -> None:
    settings = load()
    logging.basicConfig(level=settings.log_level)
    structlog.configure()
    deps = asyncio.run(_build_deps(settings))
    app = build_app(deps=deps, bearer_token=settings.bearer_token)
    uvicorn.run(
        app,
        host=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
