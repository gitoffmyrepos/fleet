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


def _require(a: dict[str, Any], key: str) -> Any:
    """Get a required arg from MCP args, or raise ToolError with a clear message."""
    if key not in a or a[key] is None or a[key] == "":
        raise ToolError(f"'{key}' argument is required")
    return a[key]


# Default project for Fleet-spawned agents when the caller doesn't pass one.
# Matches the existing dispatch_swarm default. 95% of Fleet work is on FX.
_DEFAULT_CWD = "/home/kelvin/SB-HomeLAb/FX"


def _resolve_cwd(a: dict[str, Any]) -> str:
    """Resolve the agent working directory from MCP args.

    Accepts (in priority order): cwd, workdir, repo_path. Falls back to the
    FX repo so existing callers that didn't know to pass cwd keep working —
    just from a sensible default rather than the daemon's own dir.
    """
    return a.get("cwd") or a.get("workdir") or a.get("repo_path") or _DEFAULT_CWD


def _result_dict(task_id: str, result: Any) -> dict[str, Any]:
    """Serialise a DispatchResult into the MCP response shape, including
    persistence fields (commit_sha, pushed, persistence_note) so the caller
    knows whether the agent's work landed in git."""
    return {
        "task_id": task_id,
        "ok": result.ok,
        "summary": result.summary,
        "error": result.error,
        "commit_sha": getattr(result, "commit_sha", None),
        "pushed": getattr(result, "pushed", False),
        "persistence_note": getattr(result, "persistence_note", ""),
    }


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
            "list_skills": self._list_skills,
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
        task = _require(a, "task")
        task_id = a.get("task_id") or _new_task_id()
        # Calling harness can opt into "caller classifies" mode by passing
        # defer_to_caller=true. LLM-driven harnesses (Claude Code, OpenClaw, Goose
        # with MiniMax) should pass true so Fleet doesn't burn a server-side LLM
        # call when the caller already has an LLM context. Non-LLM callers
        # (scripts, dashboards) leave it false to get Fleet's own LLM fallback.
        defer = bool(a.get("defer_to_caller", False))
        decision = await self._d.router.route(task=task, task_id=task_id, defer_to_caller=defer)
        return {
            "task_id": task_id,
            "kind": decision.kind,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "via": decision.via,
            "degraded": decision.degraded,
            "suggested_agents": decision.suggested_agents,
            "suggested_topology": decision.suggested_topology,
            "requires_caller_classification": decision.requires_caller_classification,
        }

    async def _dispatch_swarm(self, a: dict[str, Any]) -> dict[str, Any]:
        task = _require(a, "task")
        task_id = a.get("task_id") or _new_task_id()
        scope = list(a.get("scope_paths") or [])
        h = task_hash(task=task, scope_paths=scope)
        cached = await self._d.cache.lookup(h)
        if cached is not None:
            return {"task_id": task_id, "cache_hit": True, **cached}
        # workdir = project directory for spawned agents. Defaults to FX repo
        # so Claude Code finds the correct CLAUDE.md (not Fleet's). Passed as
        # both `workdir` (claude-flow --workdir flag, also used in env() for
        # PWD) AND `cwd` (subprocess CWD via base.dispatch — fixes the
        # 2026-05-10 issue where agents wrote to the daemon's CWD because
        # the env() PWD trick alone didn't propagate to syscall-level CWD).
        workdir = _resolve_cwd(a)
        isolation = a.get("isolation", "worktree")
        skill_kind = a.get("route_kind") or "swarm"
        skill_payload = await self._build_skill_payload(skill_kind, int(a.get("skill_limit", 15)))
        result = await self._d.swarm.dispatch(
            task_id=task_id,
            task=task,
            agents=int(a.get("agents", 20)),
            topology=a.get("topology", "parallel"),
            strategy=a.get("strategy", "development"),
            workdir=workdir,
            cwd=workdir,
            auto_commit=bool(a.get("auto_commit", True)),
            isolation=isolation,
            skill_header=skill_payload["header"],
            skill_roots=skill_payload["roots"],
        )
        if result.ok:
            await self._d.cache.write(task_hash_value=h, kind="swarm", summary=result.summary)
        return {
            "cache_hit": False,
            **_result_dict(task_id, result),
        }

    async def _dispatch_phase(self, a: dict[str, Any]) -> dict[str, Any]:
        task = _require(a, "task")
        task_id = a.get("task_id") or _new_task_id()
        cwd = _resolve_cwd(a)
        result = await self._d.phase.dispatch(
            task_id=task_id,
            task=task,
            stage=a.get("stage", "plan"),
            cwd=cwd,
            auto_commit=bool(a.get("auto_commit", True)),
        )
        return _result_dict(task_id, result)

    async def _dispatch_subagent(self, a: dict[str, Any]) -> dict[str, Any]:
        task = _require(a, "task")
        task_id = a.get("task_id") or _new_task_id()
        cwd = _resolve_cwd(a)
        # Default to worktree isolation so parallel subagents against the
        # same cwd don't race on `git add -A` at commit time (2026-05-10
        # incident). Caller can override with isolation=None for legacy
        # single-agent behavior.
        isolation = a.get("isolation", "worktree")
        # Resolve the unified skills catalog + filtered subset for this kind
        # so the dispatcher can inject a skill header and --add-dir for the
        # skill roots. The caller can pre-classify via route_kind; otherwise
        # default to "subagent".
        skill_kind = a.get("route_kind") or "subagent"
        skill_limit = int(a.get("skill_limit", 15))
        skill_payload = await self._build_skill_payload(skill_kind, skill_limit)
        result = await self._d.subagent.dispatch(
            task_id=task_id,
            task=task,
            agent_hint=a.get("agent_hint"),
            cwd=cwd,
            auto_commit=bool(a.get("auto_commit", True)),
            isolation=isolation,
            skill_header=skill_payload["header"],
            skill_roots=skill_payload["roots"],
        )
        return _result_dict(task_id, result)

    async def _build_skill_payload(self, kind: str, limit: int) -> dict[str, Any]:
        """Load + filter skills, return (header, roots) for dispatcher injection."""
        from .skills import filter_skills, load_catalog, render_prompt_header

        try:
            catalog = await load_catalog()
        except Exception:
            # Never block a dispatch on a skills lookup error.
            return {"header": "", "roots": []}
        filtered = filter_skills(catalog, kind=kind, limit=limit)
        return {
            "header": render_prompt_header(filtered),
            "roots": catalog.get("roots", []),
        }

    async def _dispatch_verify(self, a: dict[str, Any]) -> dict[str, Any]:
        task = _require(a, "task")
        task_id = a.get("task_id") or _new_task_id()
        cwd = _resolve_cwd(a)
        # Verification is read-only by default; don't auto-commit unless explicit
        result = await self._d.verify.dispatch(
            task_id=task_id,
            task=task,
            scope=a.get("scope"),
            cwd=cwd,
            auto_commit=bool(a.get("auto_commit", False)),
        )
        return _result_dict(task_id, result)

    async def _ship(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = a.get("task_id") or _new_task_id()
        cwd = _resolve_cwd(a)
        result = await self._d.phase.dispatch(
            task_id=task_id,
            task=a.get("task", "ship"),
            stage="ship",
            cwd=cwd,
            auto_commit=bool(a.get("auto_commit", True)),
        )
        return _result_dict(task_id, result)

    async def _status(self, a: dict[str, Any]) -> dict[str, Any]:
        kind = a.get("kind_prefix", "fleet_dispatch")
        facts = await self._d.graphiti.search_facts(kind_prefix=kind, limit=int(a.get("limit", 50)))
        return {"items": facts, "circuits": self._d.circuits.snapshot_all()}

    async def _explain(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = _require(a, "task_id")
        chain = await self._d.graphiti.search_facts(parent_task_id=task_id, limit=200)
        return {"task_id": task_id, "chain": chain}

    async def _cache_lookup(self, a: dict[str, Any]) -> dict[str, Any]:
        task = _require(a, "task")
        h = task_hash(task=task, scope_paths=list(a.get("scope_paths") or []))
        hit = await self._d.cache.lookup(h)
        return {"hash": h, "hit": hit is not None, "entry": hit}

    async def _list_agents(self, a: dict[str, Any]) -> dict[str, Any]:
        return {
            "stale": self._d.registry.is_stale(),
            "agents": [asdict(d) for d in self._d.registry.all()],
        }

    async def _list_skills(self, a: dict[str, Any]) -> dict[str, Any]:
        """Return the unified skills catalog (hermes + claude + marketplaces).

        Args (all optional):
          kind: "swarm"|"phase"|"subagent"|"verify"|"ship" — filter by task type
          tag: str — exact-match frontmatter tag
          mcp: str — required MCP server name
          limit: int (default 50) — cap results
        """
        from .skills import filter_skills, load_catalog

        catalog = await load_catalog()
        filtered = filter_skills(
            catalog,
            kind=a.get("kind"),
            tag=a.get("tag"),
            mcp=a.get("mcp"),
            limit=int(a.get("limit", 50)),
        )
        # Emit telemetry so we can later see which skills get surfaced.
        # Telemetry failure is never fatal — swallow + move on.
        import contextlib

        with contextlib.suppress(Exception):
            await self._d.telemetry.event(
                task_id=a.get("task_id") or _new_task_id(),
                kind="fleet_skills_listed",
                body={
                    "kind": a.get("kind"),
                    "tag": a.get("tag"),
                    "mcp": a.get("mcp"),
                    "returned": len(filtered),
                    "total_catalog": len(catalog.get("skills", [])),
                },
            )
        return {
            "count": len(filtered),
            "skills": filtered,
            "roots": catalog.get("roots", []),
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
        task_id = _require(a, "task_id")
        await self._d.telemetry.event(
            task_id=task_id,
            kind=a.get("kind", "fleet_external_event"),
            body=a.get("body", {}),
        )
        return {"ok": True}

    async def _cancel(self, a: dict[str, Any]) -> dict[str, Any]:
        task_id = _require(a, "task_id")
        await self._d.telemetry.event(
            task_id=task_id,
            kind="fleet_cancel_requested",
            body={"requested_by": a.get("by", "operator")},
        )
        return {"task_id": task_id, "cancel_requested": True}

    async def _circuit_close(self, a: dict[str, Any]) -> dict[str, Any]:
        name = _require(a, "name")
        ok = self._d.circuits.close(name)
        return {"name": name, "closed": bool(ok)}
