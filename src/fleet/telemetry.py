"""Append-only telemetry to Graphiti episodes."""

from __future__ import annotations

import time
from typing import Any

from .graphiti_client import GraphitiClient

MAX_VALUE_BYTES = 2048


def redact(
    obj: Any,
    _path: list[str] | None = None,
    _truncated: list[str] | None = None,
) -> Any:
    """Recursively truncate string values larger than MAX_VALUE_BYTES.

    For nested truncations the TOP-LEVEL key is what's reported in the
    output's `_truncated_keys` marker, not the full dotted path. So
    `redact({"a": {"b": <huge>}})` will yield `_truncated_keys=["a"]`, not
    `["a.b"]`. This keeps the marker stable when callers reorganise inner
    structure; downstream tooling should treat the marker as a hint, not
    a precise pointer.
    """
    if _path is None:
        _path = []
    if _truncated is None:
        _truncated = []
    if isinstance(obj, str):
        if len(obj.encode("utf-8")) > MAX_VALUE_BYTES:
            if _path:
                _truncated.append(_path[0])
            return obj.encode("utf-8")[:MAX_VALUE_BYTES].decode("utf-8", "ignore")
        return obj
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            out[k] = redact(v, [*_path, k], _truncated)
        if not _path and _truncated:
            out["_truncated_keys"] = sorted(set(_truncated))
        return out
    if isinstance(obj, list):
        return [redact(v, [*_path, str(i)], _truncated) for i, v in enumerate(obj)]
    return obj


class Telemetry:
    def __init__(self, graphiti: GraphitiClient):
        self._g = graphiti
        self._starts: dict[str, float] = {}

    async def start(self, *, task_id: str, kind: str, body: dict[str, Any]) -> str:
        self._starts[task_id] = time.monotonic()
        return await self._g.add_episode(
            kind="fleet_dispatch_started",
            parent_task_id=task_id,
            body=redact({**body, "dispatch_kind": kind}),
        )

    async def end(self, *, task_id: str, ok: bool, body: dict[str, Any]) -> str:
        t0 = self._starts.pop(task_id, None)
        elapsed = (time.monotonic() - t0) if t0 is not None else 0.0
        return await self._g.add_episode(
            kind="fleet_dispatch_completed" if ok else "fleet_dispatch_failed",
            parent_task_id=task_id,
            body=redact({**body, "duration_seconds": round(elapsed, 3), "ok": ok}),
        )

    async def failure(self, *, task_id: str, reason: str, body: dict[str, Any]) -> str:
        self._starts.pop(task_id, None)
        return await self._g.add_episode(
            kind="fleet_dispatch_failed",
            parent_task_id=task_id,
            body=redact({**body, "reason": reason, "ok": False}),
        )

    async def event(self, *, task_id: str, kind: str, body: dict[str, Any]) -> str:
        return await self._g.add_episode(
            kind=kind,
            parent_task_id=task_id,
            body=redact(body),
        )
