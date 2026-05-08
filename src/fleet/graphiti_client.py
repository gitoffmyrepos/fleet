"""Async client for the real Graphiti MCP JSON-RPC server.

Talks Streamable-HTTP MCP at `POST /mcp` with `Accept: text/event-stream` and
parses the SSE-framed JSON-RPC response.

Public API is preserved across protocol layers — `add_episode`, `search_facts`,
and `get_by_hash` map onto Graphiti's actual tools (`add_memory`,
`get_episodes`).

TODO(Phase 14 hardening): exception messages currently embed up to 200 chars
of the upstream response body for debugging (`r.text[:200]`). For internal
service-to-service use this is acceptable, but it should be reconsidered if
exception strings ever reach external log sinks (e.g. Grafana Loki). Options:
(1) drop the body excerpt from the exception, log it via structlog DEBUG;
(2) redact known-sensitive patterns. Tracked alongside the SecretStr TODO in
config.py.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

# All Fleet-emitted episodes share this group_id by default.
FLEET_GROUP = "fleet"
# Cache entries use a per-hash group so lookup-by-hash is O(N=1) within the group.
CACHE_GROUP_PREFIX = "fleet_cache"


def _parse_sse_or_json(body: str) -> dict[str, Any]:
    """Graphiti returns either plain JSON or SSE-framed JSON. Handle both."""
    body = body.strip()
    if body.startswith("event:") or body.startswith("data:"):
        for line in body.splitlines():
            if line.startswith("data:"):
                parsed: dict[str, Any] = json.loads(line[5:].strip())
                return parsed
        raise RuntimeError("graphiti: SSE response with no data line")
    parsed_json: dict[str, Any] = json.loads(body)
    return parsed_json


class GraphitiClient:
    def __init__(self, url: str, bearer: str = "", timeout: float = 30.0) -> None:
        # Graphiti MCP root: e.g. http://host:port/mcp (no trailing slash).
        self._url = url.rstrip("/")
        self._bearer = bearer
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
        }
        if self._bearer:
            h["authorization"] = f"Bearer {self._bearer}"
        return h

    async def _call_tool(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        rpc = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex[:12],
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(self._url, headers=self._headers(), content=json.dumps(rpc))
            if r.status_code >= 400:
                raise RuntimeError(f"graphiti {tool} {r.status_code}: {r.text[:200]}")
            payload = _parse_sse_or_json(r.text)
            if "error" in payload:
                raise RuntimeError(f"graphiti {tool} jsonrpc error: {payload['error']}")
            outer = payload.get("result", {})
            if not isinstance(outer, dict):
                return {}
            # Real Graphiti MCP wraps the typed result under `structuredContent.result`.
            # Older / fixture-style servers may put it under `result` directly. Try both.
            sc = outer.get("structuredContent")
            if isinstance(sc, dict):
                inner = sc.get("result", sc)
                return inner if isinstance(inner, dict) else {}
            inner = outer.get("result", outer)
            return inner if isinstance(inner, dict) else {}

    async def add_episode(
        self,
        *,
        kind: str,
        body: dict[str, Any],
        parent_task_id: str | None,
        correlation_id: str | None = None,
    ) -> str:
        """Write a Fleet-shaped record to Graphiti via add_memory.

        The Graphiti memory model is `name + episode_body + group_id`; we encode
        Fleet's `kind/body/parent_task_id` triple into a JSON episode_body and use
        `kind` as the episode name. For cache entries, the group_id is namespaced
        per-hash so `get_by_hash` becomes a single-record lookup.
        """
        episode_uuid = correlation_id or uuid.uuid4().hex
        group_id = (
            f"{CACHE_GROUP_PREFIX}:{body['task_hash']}"
            if kind == "fleet_cache_entry" and "task_hash" in body
            else FLEET_GROUP
        )
        episode = {"body": body, "parent_task_id": parent_task_id, "kind": kind}
        await self._call_tool(
            "add_memory",
            {
                "name": kind,
                "episode_body": json.dumps(episode, default=str),
                "group_id": group_id,
                "source": "json",
                "source_description": "fleet-mcp",
                "uuid": episode_uuid,
            },
        )
        return episode_uuid

    async def search_facts(
        self,
        *,
        parent_task_id: str | None = None,
        kind_prefix: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Return a list of Fleet-shaped fact dicts.

        Fetches recent Graphiti episodes from the `fleet` group and reconstructs
        the Fleet record by parsing each episode's JSON body. Filters by
        `parent_task_id` or `kind_prefix` client-side.
        """
        result = await self._call_tool(
            "get_episodes",
            {"group_ids": [FLEET_GROUP], "max_episodes": max(limit * 2, 50)},
        )
        episodes = result.get("episodes", []) if isinstance(result, dict) else []
        out: list[dict[str, Any]] = []
        for ep in episodes:
            content = ep.get("content") or ep.get("episode_body") or ep.get("body")
            parsed: dict[str, Any] = {}
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                except json.JSONDecodeError:
                    parsed = {}
            elif isinstance(content, dict):
                parsed = content
            kind = parsed.get("kind") or ep.get("name", "")
            if kind_prefix and not kind.startswith(kind_prefix):
                continue
            if parent_task_id and parsed.get("parent_task_id") != parent_task_id:
                continue
            out.append(
                {
                    "id": ep.get("uuid") or ep.get("id"),
                    "kind": kind,
                    "body": parsed.get("body") if isinstance(parsed.get("body"), dict) else parsed,
                    "parent_task_id": parsed.get("parent_task_id"),
                }
            )
            if len(out) >= limit:
                break
        return out

    async def get_by_hash(self, *, task_hash: str) -> dict[str, Any] | None:
        """Cache lookup by hash. Uses a per-hash group_id for O(1) retrieval."""
        result = await self._call_tool(
            "get_episodes",
            {"group_ids": [f"{CACHE_GROUP_PREFIX}:{task_hash}"], "max_episodes": 1},
        )
        episodes = result.get("episodes", []) if isinstance(result, dict) else []
        if not episodes:
            return None
        ep = episodes[0]
        content = ep.get("content") or ep.get("episode_body") or ep.get("body")
        parsed: dict[str, Any] = {}
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return None
        elif isinstance(content, dict):
            parsed = content
        return {"id": ep.get("uuid") or ep.get("id"), "body": parsed.get("body", parsed)}
