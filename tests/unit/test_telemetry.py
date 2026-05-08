from unittest.mock import AsyncMock

import pytest

from fleet.telemetry import Telemetry, redact


def test_redact_truncates_long_strings() -> None:
    long = "x" * 5000
    out = redact({"output": long, "small": "ok"})
    assert len(out["output"]) <= 2048
    assert out["small"] == "ok"
    assert out["_truncated_keys"] == ["output"]


def test_redact_handles_nested() -> None:
    src = {"a": {"b": "y" * 5000}}
    out = redact(src)
    assert len(out["a"]["b"]) <= 2048


@pytest.mark.asyncio
async def test_start_and_end_emit_two_episodes() -> None:
    fake = AsyncMock()
    fake.add_episode = AsyncMock(side_effect=["ep_start", "ep_end"])
    t = Telemetry(graphiti=fake)
    task_id = "task_abc"
    await t.start(task_id=task_id, kind="swarm", body={"task": "audit"})
    await t.end(task_id=task_id, ok=True, body={"summary": "done"})
    assert fake.add_episode.await_count == 2
    first = fake.add_episode.await_args_list[0]
    assert first.kwargs["kind"] == "fleet_dispatch_started"
    assert first.kwargs["parent_task_id"] == task_id
    second = fake.add_episode.await_args_list[1]
    assert second.kwargs["kind"] == "fleet_dispatch_completed"


@pytest.mark.asyncio
async def test_failure_records_failure_episode() -> None:
    fake = AsyncMock()
    fake.add_episode = AsyncMock(return_value="ep")
    t = Telemetry(graphiti=fake)
    await t.failure(task_id="t", reason="boom", body={})
    fake.add_episode.assert_awaited_once()
    call = fake.add_episode.await_args
    assert call.kwargs["kind"] == "fleet_dispatch_failed"
    assert call.kwargs["body"]["reason"] == "boom"


@pytest.mark.asyncio
async def test_event_emits_arbitrary_kind() -> None:
    fake = AsyncMock()
    fake.add_episode = AsyncMock(return_value="ep_e")
    t = Telemetry(graphiti=fake)
    eid = await t.event(task_id="t", kind="custom_kind", body={"x": 1})
    assert eid == "ep_e"
    call = fake.add_episode.await_args
    assert call.kwargs["kind"] == "custom_kind"
    assert call.kwargs["body"] == {"x": 1}


def test_redact_handles_lists() -> None:
    out = redact([{"a": "b"}, "x" * 5000])
    assert isinstance(out, list)
    assert len(out[1]) <= 2048


def test_redact_passes_through_non_strings() -> None:
    assert redact(42) == 42
    assert redact(True) is True
    assert redact(None) is None


@pytest.mark.asyncio
async def test_end_without_start_uses_zero_elapsed() -> None:
    fake = AsyncMock()
    fake.add_episode = AsyncMock(return_value="ep")
    t = Telemetry(graphiti=fake)
    await t.end(task_id="never_started", ok=False, body={})
    call = fake.add_episode.await_args
    assert call.kwargs["kind"] == "fleet_dispatch_failed"
    assert call.kwargs["body"]["duration_seconds"] == 0.0
    assert call.kwargs["body"]["ok"] is False


@pytest.mark.asyncio
async def test_failure_after_start_drains_starts() -> None:
    fake = AsyncMock()
    fake.add_episode = AsyncMock(return_value="ep")
    t = Telemetry(graphiti=fake)
    await t.start(task_id="t", kind="k", body={})
    await t.failure(task_id="t", reason="oops", body={})
    assert "t" not in t._starts


def test_redact_nested_truncation_reports_top_level_key() -> None:
    out = redact({"a": {"b": "x" * 5000}})
    assert out["_truncated_keys"] == ["a"]
