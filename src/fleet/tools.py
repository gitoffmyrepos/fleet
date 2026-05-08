"""MCP tool registry + dispatch table."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from .cache import task_hash


class ToolError(RuntimeError):
    """Raised when a requested tool name is unknown."""


def _new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


class ToolRegistry:
    """Dispatch table mapping MCP tool names to handler coroutines.

    Each handler accepts a single dict of arguments and returns a dict of
    results. The registry holds a reference to a shared `deps` namespace
    where each backend (router/cache/registry/dispatchers/telemetry/graphiti/
    circuits) has been pre-wired.
    """

    def __init__(self, deps: Any) -> None:
        self._d = deps
        self._handlers = {
            "route": self._route,
            "dispatch_swarm": self._dispatch_swarm,
            "dispatch_phase": self._dispatch_phase,
            "dispatch_subagent": self._dispatch_subagent,
            "dispatch_verify": self._dispatch_verify,
            "ship": self._ship,
            "status": self._status,
            "explain": self._explain,
            "cache_lookup": self._cache_lookup,
            "list_agents": self._list_agents,
            "register_agent": self._register_agent,
            "telemetry": self._telemetry,
            "cancel": self._cancel,
            "circuit_close": self._circuit_close,
        }

    def list_tool_names(self) -> list[str]:
        return list(self._handlers)

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        h = self._handlers.get(name)
        if h is None:
            raise ToolError(f"unknown tool: {name}")
        return await h(args)

    async def _route(self, a: dict[str, Any]) -> dict[str, Any]:
        task = a["task"]
        task_id = a.get("task_id") or _new_task_id()
        decision = await self._d.router.route(task=task, task_id=task_id)
        return {
            "task_id": task_id,
            "kind": decision.kind,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "via": decision.via,
            "degraded": decision.degraded,
            "suggested_agents": decision.suggested_agents,
            "suggested_topology": decision.suggested_topology,
        }

    async def _dispatch_swarm(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = a.get("task_id") or _new_task_id()
        scope = list(a.get("scope_paths") or [])
        h = task_hash(task=a["task"], scope_paths=scope)
        cached = await self._d.cache.lookup(h)
        if cached is not None:
            return {"task_id": task_id, "cache_hit": True, **cached}
        result = await self._d.swarm.dispatch(
            task_id=task_id,
            task=a["task"],
            agents=int(a.get("agents", 20)),
            topology=a.get("topology", "parallel"),
            strategy=a.get("strategy", "development"),
        )
        if result.ok:
            await self._d.cache.write(task_hash_value=h, kind="swarm", summary=result.summary)
        return {
            "task_id": task_id,
            "cache_hit": False,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
        }

    async def _dispatch_phase(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = a.get("task_id") or _new_task_id()
        result = await self._d.phase.dispatch(
            task_id=task_id, task=a["task"], stage=a.get("stage", "plan")
        )
        return {
            "task_id": task_id,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
        }

    async def _dispatch_subagent(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = a.get("task_id") or _new_task_id()
        result = await self._d.subagent.dispatch(
            task_id=task_id, task=a["task"], agent_hint=a.get("agent_hint")
        )
        return {
            "task_id": task_id,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
        }

    async def _dispatch_verify(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = a.get("task_id") or _new_task_id()
        result = await self._d.verify.dispatch(
            task_id=task_id, task=a["task"], scope=a.get("scope")
        )
        return {
            "task_id": task_id,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
        }

    async def _ship(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = a.get("task_id") or _new_task_id()
        result = await self._d.phase.dispatch(
            task_id=task_id, task=a.get("task", "ship"), stage="ship"
        )
        return {
            "task_id": task_id,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
        }

    async def _status(self, a: dict[str, Any]) -> dict[str, Any]:
        kind = a.get("kind_prefix", "fleet_dispatch")
        facts = await self._d.graphiti.search_facts(kind_prefix=kind, limit=int(a.get("limit", 50)))
        return {"items": facts, "circuits": self._d.circuits.snapshot_all()}

    async def _explain(self, a: dict[str, Any]) -> dict[str, Any]:
        chain = await self._d.graphiti.search_facts(parent_task_id=a["task_id"], limit=200)
        return {"task_id": a["task_id"], "chain": chain}

    async def _cache_lookup(self, a: dict[str, Any]) -> dict[str, Any]:
        h = task_hash(task=a["task"], scope_paths=list(a.get("scope_paths") or []))
        hit = await self._d.cache.lookup(h)
        return {"hash": h, "hit": hit is not None, "entry": hit}

    async def _list_agents(self, a: dict[str, Any]) -> dict[str, Any]:
        agents = self._d.registry.all()
        return {
            "stale": self._d.registry.is_stale(),
            "agents": [asdict(d) if hasattr(d, "__dataclass_fields__") else d for d in agents],
        }

    async def _register_agent(self, a: dict[str, Any]) -> dict[str, Any]:
        await self._d.telemetry.event(
            task_id=a.get("task_id") or _new_task_id(),
            kind="fleet_register_agent_request",
            body=a,
        )
        return {
            "accepted": False,
            "reason": "registry is filesystem-driven in v1; drop a file in the source path",
        }

    async def _telemetry(self, a: dict[str, Any]) -> dict[str, Any]:
        await self._d.telemetry.event(
            task_id=a["task_id"],
            kind=a.get("kind", "fleet_external_event"),
            body=a.get("body", {}),
        )
        return {"ok": True}

    async def _cancel(self, a: dict[str, Any]) -> dict[str, Any]:
        await self._d.telemetry.event(
            task_id=a["task_id"],
            kind="fleet_cancel_requested",
            body={"requested_by": a.get("by", "operator")},
        )
        return {"task_id": a["task_id"], "cancel_requested": True}

    async def _circuit_close(self, a: dict[str, Any]) -> dict[str, Any]:
        ok = self._d.circuits.close(a["name"])
        return {"name": a["name"], "closed": bool(ok)}
