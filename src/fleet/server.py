"""FastAPI server exposing Fleet MCP tools over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from .tools import ToolError, ToolRegistry


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

    from fastapi.responses import HTMLResponse

    from .dashboard import metrics_json, render_html

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard() -> str:
        return await render_html(deps=deps)

    @app.get("/dashboard/metrics.json")
    async def dashboard_metrics() -> dict[str, Any]:
        return await metrics_json(deps=deps)

    return app
