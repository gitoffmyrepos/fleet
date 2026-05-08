"""In-memory stub of Graphiti HTTP API for integration tests."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
_episodes: dict[str, dict] = {}
_by_hash: dict[str, str] = {}


class EpisodeIn(BaseModel):
    kind: str
    body: dict
    parent_task_id: str | None = None
    correlation_id: str | None = None


@app.post("/mcp/episodes")
async def add_episode(ep: EpisodeIn) -> dict:
    eid = f"ep_{uuid.uuid4().hex[:8]}"
    _episodes[eid] = {"id": eid, **ep.model_dump()}
    if ep.kind == "fleet_cache_entry":
        _by_hash[ep.body["task_hash"]] = eid
    return {"id": eid}


@app.get("/mcp/facts")
async def search_facts(
    parent_task_id: str | None = None,
    kind_prefix: str | None = None,
    limit: int = 200,
) -> dict:
    items = list(_episodes.values())
    if parent_task_id:
        items = [e for e in items if e.get("parent_task_id") == parent_task_id]
    if kind_prefix:
        items = [e for e in items if e["kind"].startswith(kind_prefix)]
    return {"facts": items[:limit]}


@app.get("/mcp/episodes/by-hash")
async def by_hash(task_hash: str) -> dict:
    eid = _by_hash.get(task_hash)
    if not eid:
        return {"error": "not_found"}
    return _episodes[eid]
