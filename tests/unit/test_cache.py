import time
from unittest.mock import AsyncMock

import pytest

from fleet.cache import Cache, task_hash


def test_hash_stable_for_same_input() -> None:
    h1 = task_hash(task="audit svc", scope_paths=["a/b", "c/d"])
    h2 = task_hash(task="audit svc", scope_paths=["a/b", "c/d"])
    assert h1 == h2
    assert len(h1) == 64


def test_hash_path_order_independent() -> None:
    h1 = task_hash(task="t", scope_paths=["a", "b"])
    h2 = task_hash(task="t", scope_paths=["b", "a"])
    assert h1 == h2


def test_hash_whitespace_normalized() -> None:
    h1 = task_hash(task="audit  svc\n", scope_paths=[])
    h2 = task_hash(task="audit svc", scope_paths=[])
    assert h1 == h2


def test_hash_case_normalized() -> None:
    h1 = task_hash(task="Audit Svc", scope_paths=[])
    h2 = task_hash(task="audit svc", scope_paths=[])
    assert h1 == h2


def test_hash_different_task_different_hash() -> None:
    assert task_hash(task="a", scope_paths=[]) != task_hash(task="b", scope_paths=[])


@pytest.fixture
def fake_graphiti() -> AsyncMock:
    g = AsyncMock()
    g.add_episode = AsyncMock(return_value="ep_1")
    g.get_by_hash = AsyncMock(return_value=None)
    return g


@pytest.fixture
def cache(fake_graphiti: AsyncMock) -> Cache:
    tel = AsyncMock()
    return Cache(graphiti=fake_graphiti, telemetry=tel, ttl_seconds=3600)


@pytest.mark.asyncio
async def test_lookup_miss_returns_none(cache: Cache) -> None:
    assert await cache.lookup("h" * 64) is None


@pytest.mark.asyncio
async def test_write_then_lookup_hits(cache: Cache, fake_graphiti: AsyncMock) -> None:
    await cache.write(
        task_hash_value="h" * 64,
        kind="swarm",
        summary={"text": "done"},
    )
    fake_graphiti.add_episode.assert_awaited_once()
    call = fake_graphiti.add_episode.await_args
    assert call.kwargs["kind"] == "fleet_cache_entry"
    assert call.kwargs["body"]["task_hash"] == "h" * 64


@pytest.mark.asyncio
async def test_lookup_returns_unexpired_entry(fake_graphiti: AsyncMock) -> None:
    fake_graphiti.get_by_hash = AsyncMock(
        return_value={
            "id": "ep_x",
            "body": {
                "task_hash": "h",
                "kind": "swarm",
                "summary": {"text": "cached"},
                "stored_at": time.time() - 60,
            },
        }
    )
    tel = AsyncMock()
    c = Cache(graphiti=fake_graphiti, telemetry=tel, ttl_seconds=3600)
    hit = await c.lookup("h")
    assert hit is not None
    assert hit["summary"]["text"] == "cached"
    assert hit["age_seconds"] >= 60


@pytest.mark.asyncio
async def test_lookup_expired_returns_none(fake_graphiti: AsyncMock) -> None:
    fake_graphiti.get_by_hash = AsyncMock(
        return_value={
            "id": "ep_x",
            "body": {
                "task_hash": "h",
                "kind": "swarm",
                "summary": {"text": "stale"},
                "stored_at": time.time() - 7200,
            },
        }
    )
    tel = AsyncMock()
    c = Cache(graphiti=fake_graphiti, telemetry=tel, ttl_seconds=3600)
    assert await c.lookup("h") is None


@pytest.mark.asyncio
async def test_lookup_corrupt_evicts_and_returns_none(fake_graphiti: AsyncMock) -> None:
    fake_graphiti.get_by_hash = AsyncMock(
        return_value={
            "id": "ep_x",
            "body": {"corrupt": True},
        }
    )
    tel = AsyncMock()
    c = Cache(graphiti=fake_graphiti, telemetry=tel, ttl_seconds=3600)
    assert await c.lookup("h") is None
    tel.event.assert_awaited()
    call = tel.event.await_args
    assert call.kwargs["kind"] == "fleet_cache_corrupt"
