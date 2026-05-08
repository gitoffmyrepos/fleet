"""FastAPI server exposing Fleet MCP tools over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .tools import ToolError, ToolRegistry

_TOOL_DESCRIPTIONS: dict[str, str] = {
    "route": (
        "Auto-classify a task into swarm/phase/subagent/verify/ship and "
        "return the dispatch decision."
    ),
    "dispatch_swarm": (
        "Fan-out parallel work via claude-flow. "
        "Args: task, agents=20, topology=parallel, strategy=development."
    ),
    "dispatch_phase": (
        "Drive a gsd lifecycle stage (plan/execute/verify/discuss/ship) for multi-step builds."
    ),
    "dispatch_subagent": (
        "Run a single isolated subagent via claude --print. Args: task, agent_hint?"
    ),
    "dispatch_verify": ("Run superpowers verification-before-completion. Args: task, scope?"),
    "ship": "Run gsd:ship — final verification + release prep.",
    "status": "Show recent dispatches + circuit-breaker states.",
    "explain": ("Return the full citation chain for a task (route → dispatch → telemetry events)."),
    "cache_lookup": (
        "Check the deterministic-hash cache for a previous dispatch. Args: task, scope_paths?"
    ),
    "list_agents": "Return the union agent registry across ruflo/superpowers/claude/gsd.",
    "register_agent": ("Log an agent-registration request (registry is filesystem-driven in v1)."),
    "telemetry": "Emit a custom telemetry event to Graphiti. Args: task_id, kind?, body?",
    "cancel": "Request cancellation of an in-flight dispatch. Args: task_id, by?",
    "circuit_close": (
        "Manually close an upstream circuit breaker. Args: name (ruflo|superpowers|gsd)."
    ),
}


class ToolCallBody(BaseModel):
    name: str
    arguments: dict[str, Any] = {}


def build_app(*, deps: Any, bearer_token: str) -> FastAPI:
    app = FastAPI(title="fleet-mcp", version="0.1.0")
    tools = ToolRegistry(deps)

    def _auth(authorization: str | None) -> None:
        if not bearer_token:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, "missing bearer")
        if authorization.removeprefix("Bearer ").strip() != bearer_token:
            raise HTTPException(401, "bad bearer")

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": "0.1.0"}

    @app.get("/mcp/tools/list")
    async def list_tools(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        _auth(authorization)
        return {"tools": tools.list_tool_names()}

    @app.post("/mcp/tools/call")
    async def call_tool(
        body: ToolCallBody,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _auth(authorization)
        try:
            result = await tools.call(body.name, body.arguments)
        except ToolError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": body.name, "result": result}

    @app.post("/mcp")
    async def mcp_jsonrpc(
        body: dict[str, Any],
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        """Standard MCP JSON-RPC 2.0 entrypoint for Claude Code / Goose / OpenClaw."""
        _auth(authorization)
        rpc_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}

        try:
            if method == "initialize":
                result: dict[str, Any] = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fleet", "version": "0.1.0"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": n,
                            "description": _TOOL_DESCRIPTIONS.get(n, ""),
                            "inputSchema": {"type": "object", "additionalProperties": True},
                        }
                        for n in tools.list_tool_names()
                    ]
                }
            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments") or {}
                try:
                    raw = await tools.call(tool_name, tool_args)
                except ToolError as e:
                    return {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {"code": -32602, "message": str(e)},
                    }
                import json as _json

                result = {
                    "content": [{"type": "text", "text": _json.dumps(raw, default=str)}],
                    "structuredContent": {"result": raw},
                    "isError": False,
                }
            elif method == "notifications/initialized":
                # Notification — no response expected
                return {"jsonrpc": "2.0", "id": rpc_id, "result": {}}
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32601, "message": f"method not found: {method}"},
                }
        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32603, "message": f"internal error: {type(e).__name__}: {e}"},
            }

        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    from fastapi.responses import HTMLResponse

    from .dashboard import metrics_json, render_html

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        return await render_html(deps=deps)

    @app.get("/dashboard/metrics.json")
    async def dashboard_metrics() -> dict[str, Any]:
        return await metrics_json(deps=deps)

    return app
