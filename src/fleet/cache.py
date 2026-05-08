"""Hash-keyed result memoization backed by Graphiti episodes."""

from __future__ import annotations

import hashlib
import re
import time
from typing import Any, ClassVar

from .graphiti_client import GraphitiClient
from .telemetry import Telemetry

_WS_RE = re.compile(r"\s+")


def _normalize(s: str) -> str:
    return _WS_RE.sub(" ", s.strip().lower())


def task_hash(*, task: str, scope_paths: list[str]) -> str:
    canon = _normalize(task) + "\n" + "\n".join(sorted(_normalize(p) for p in scope_paths))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


class Cache:
    """Hash-keyed result memoization with TTL + corrupt-entry eviction."""

    REQUIRED_KEYS: ClassVar[set[str]] = {"task_hash", "kind", "summary", "stored_at"}

    def __init__(self, graphiti: GraphitiClient, telemetry: Telemetry, ttl_seconds: int) -> None:
        self._g = graphiti
        self._t = telemetry
        self._ttl = ttl_seconds

    async def lookup(self, task_hash_value: str) -> dict[str, Any] | None:
        ep = await self._g.get_by_hash(task_hash=task_hash_value)
        if ep is None:
            return None
        body = ep.get("body") or {}
        if not self.REQUIRED_KEYS.issubset(body.keys()):
            await self._t.event(
                task_id=task_hash_value,
                kind="fleet_cache_corrupt",
                body={
                    "episode_id": ep.get("id"),
                    "missing_keys": sorted(self.REQUIRED_KEYS - set(body)),
                },
            )
            return None
        age = time.time() - float(body["stored_at"])
        if age > self._ttl:
            return None
        return {
            "kind": body["kind"],
            "summary": body["summary"],
            "age_seconds": int(age),
            "episode_id": ep.get("id"),
        }

    async def write(self, *, task_hash_value: str, kind: str, summary: dict[str, Any]) -> str:
        return await self._g.add_episode(
            kind="fleet_cache_entry",
            parent_task_id=task_hash_value,
            body={
                "task_hash": task_hash_value,
                "kind": kind,
                "summary": summary,
                "stored_at": time.time(),
            },
        )
