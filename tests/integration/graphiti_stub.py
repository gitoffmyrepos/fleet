"""In-memory stub of the Graphiti MCP JSON-RPC server for integration tests.

Speaks the same protocol as `src/fleet/graphiti_client.py`: a single
`POST /mcp` endpoint receiving JSON-RPC 2.0 requests
(`{"jsonrpc":"2.0","id":<hex>,"method":"tools/call","params":{"name":<tool>,
"arguments":{...}}}`) and replying with
`{"jsonrpc":"2.0","id":<same id>,"result":{"structuredContent":{"result":<payload>}}}`.

Two tools are implemented, matching `GraphitiClient.add_episode` /
`search_facts` / `get_by_hash`:

- `add_memory`: arguments name/episode_body/group_id/source — stores
  `{"uuid": "ep_<hex8>", "name": <name>, "content": <episode_body>,
  "group_id": <group_id>}` and returns payload `{"uuid": <uuid>}`.
- `get_episodes`: arguments group_ids (list) and max_episodes (int) —
  returns payload `{"episodes": [...]}` with stored episodes whose
  group_id is in group_ids, newest first, capped at max_episodes.

`initialize` and `tools/list` are answered minimally so MCP handshakes
do not 404.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI

app = FastAPI()
_episodes: list[dict] = []


def _envelope(rpc: dict, payload: dict) -> dict:
    """Wrap a tool payload in the JSON-RPC 2.0 result envelope the client reads."""
    return {
        "jsonrpc": "2.0",
        "id": rpc.get("id"),
        "result": {"structuredContent": {"result": payload}},
    }


def _tool_add_memory(arguments: dict) -> dict:
    ep_uuid = f"ep_{uuid.uuid4().hex[:8]}"
    _episodes.append(
        {
            "uuid": ep_uuid,
            "name": arguments.get("name"),
            "content": arguments.get("episode_body"),
            "group_id": arguments.get("group_id"),
        }
    )
    return {"uuid": ep_uuid}


def _tool_get_episodes(arguments: dict) -> dict:
    group_ids = arguments.get("group_ids") or []
    max_episodes = int(arguments.get("max_episodes", 0))
    matches = [ep for ep in _episodes if ep["group_id"] in group_ids]
    matches.reverse()  # newest first
    if max_episodes > 0:
        matches = matches[:max_episodes]
    return {"episodes": matches}


_TOOLS = {
    "add_memory": _tool_add_memory,
    "get_episodes": _tool_get_episodes,
}


@app.post("/mcp")
async def mcp(rpc: dict) -> dict:
    method = rpc.get("method")
    params = rpc.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": rpc.get("id"),
            "result": {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "graphiti-stub", "version": "0.1.0"},
            },
        }

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": rpc.get("id"),
            "result": {"tools": [{"name": name, "description": name} for name in _TOOLS]},
        }

    if method == "tools/call":
        tool = params.get("name")
        handler = _TOOLS.get(tool)
        if handler is None:
            return {
                "jsonrpc": "2.0",
                "id": rpc.get("id"),
                "error": {"code": -32601, "message": f"unknown tool: {tool!r}"},
            }
        return _envelope(rpc, handler(params.get("arguments") or {}))

    return {
        "jsonrpc": "2.0",
        "id": rpc.get("id"),
        "error": {"code": -32601, "message": f"unknown method: {method!r}"},
    }


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
