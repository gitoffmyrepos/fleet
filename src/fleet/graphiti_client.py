"""Thin async client over the Graphiti MCP HTTP surface.

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
from typing import Any

import httpx


class GraphitiClient:
    def __init__(self, url: str, bearer: str = "", timeout: float = 10.0) -> None:
        self._url = url.rstrip("/")
        self._bearer = bearer
        self._timeout = timeout

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        if self._bearer:
            h["authorization"] = f"Bearer {self._bearer}"
        return h

    async def add_episode(
        self,
        *,
        kind: str,
        body: dict[str, Any],
        parent_task_id: str | None,
        correlation_id: str | None = None,
    ) -> str:
        payload = {
            "kind": kind,
            "body": body,
            "parent_task_id": parent_task_id,
            "correlation_id": correlation_id,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.post(
                f"{self._url}/episodes",
                headers=self._headers(),
                content=json.dumps(payload, separators=(",", ":")),
            )
            if r.status_code >= 400:
                raise RuntimeError(f"graphiti add_episode {r.status_code}: {r.text[:200]}")
            data = r.json()
            if "id" not in data:
                raise RuntimeError(
                    f"graphiti add_episode: response missing 'id' field (status {r.status_code})"
                )
            return str(data["id"])

    async def search_facts(
        self,
        *,
        parent_task_id: str | None = None,
        kind_prefix: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if parent_task_id:
            params["parent_task_id"] = parent_task_id
        if kind_prefix:
            params["kind_prefix"] = kind_prefix
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.get(f"{self._url}/facts", headers=self._headers(), params=params)
            if r.status_code >= 400:
                raise RuntimeError(f"graphiti search_facts {r.status_code}: {r.text[:200]}")
            facts = r.json().get("facts", [])
            return list(facts)

    async def get_by_hash(self, *, task_hash: str) -> dict[str, Any] | None:
        async with httpx.AsyncClient(timeout=self._timeout) as cx:
            r = await cx.get(
                f"{self._url}/episodes/by-hash",
                headers=self._headers(),
                params={"task_hash": task_hash},
            )
            if r.status_code == 404:
                return None
            if r.status_code >= 400:
                raise RuntimeError(f"graphiti get_by_hash {r.status_code}: {r.text[:200]}")
            ep: dict[str, Any] = r.json()
            return ep
