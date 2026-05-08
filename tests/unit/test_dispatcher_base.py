from typing import Any
from unittest.mock import AsyncMock

import pytest

from fleet.circuit import CircuitRegistry
from fleet.dispatcher.base import DispatcherBase, DispatchResult


@pytest.fixture
def reg() -> CircuitRegistry:
    return CircuitRegistry(failure_threshold=3, window_seconds=600, cooldown_seconds=300)


@pytest.fixture
def tel() -> AsyncMock:
    t = AsyncMock()
    t.start = AsyncMock(return_value="ep_s")
    t.end = AsyncMock(return_value="ep_e")
    t.failure = AsyncMock(return_value="ep_f")
    t.event = AsyncMock(return_value="ep_v")
    return t


class _Echo(DispatcherBase):
    upstream_name = "echo"

    def cli_args(self, **kw: Any) -> list[str]:
        return ["/bin/sh", "-c", f"echo '{kw['msg']}'"]

    def parse_summary(self, stdout: str, **kw: Any) -> dict[str, Any]:
        return {"out": stdout.strip()}


@pytest.mark.asyncio
async def test_happy_path_echo(reg: CircuitRegistry, tel: AsyncMock) -> None:
    d = _Echo(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(task_id="t1", msg="hello")
    assert isinstance(result, DispatchResult)
    assert result.ok is True
    assert "hello" in result.summary["out"]
    tel.start.assert_awaited()
    tel.end.assert_awaited()


@pytest.mark.asyncio
async def test_nonzero_exit_marks_failure(reg: CircuitRegistry, tel: AsyncMock) -> None:
    class _Fail(DispatcherBase):
        upstream_name = "fail"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "exit 7"]

        def parse_summary(self, stdout: str, **kw: Any) -> dict[str, Any]:
            return {}

    d = _Fail(circuits=reg, telemetry=tel, timeout_seconds=5)
    result = await d.dispatch(task_id="t2")
    assert result.ok is False
    assert "exit code 7" in result.error or "exit 7" in result.error
    tel.failure.assert_awaited()


@pytest.mark.asyncio
async def test_timeout_returns_partial(reg: CircuitRegistry, tel: AsyncMock) -> None:
    class _Sleep(DispatcherBase):
        upstream_name = "sleep"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "sleep 3"]

        def parse_summary(self, stdout: str, **kw: Any) -> dict[str, Any]:
            return {}

    d = _Sleep(circuits=reg, telemetry=tel, timeout_seconds=1)
    result = await d.dispatch(task_id="t3")
    assert result.ok is False
    assert "timeout" in result.error.lower()


@pytest.mark.asyncio
async def test_open_circuit_blocks_dispatch(reg: CircuitRegistry, tel: AsyncMock) -> None:
    cb = reg.get("echo")
    for _ in range(3):
        cb.record_failure()
    d = _Echo(circuits=reg, telemetry=tel, timeout_seconds=10)
    result = await d.dispatch(task_id="t4", msg="hi")
    assert result.ok is False
    assert "circuit" in result.error.lower()


@pytest.mark.asyncio
async def test_3_failures_trip_breaker(reg: CircuitRegistry, tel: AsyncMock) -> None:
    class _Fail(DispatcherBase):
        upstream_name = "trip"

        def cli_args(self, **kw: Any) -> list[str]:
            return ["/bin/sh", "-c", "exit 1"]

        def parse_summary(self, stdout: str, **kw: Any) -> dict[str, Any]:
            return {}

    d = _Fail(circuits=reg, telemetry=tel, timeout_seconds=5)
    for i in range(3):
        await d.dispatch(task_id=f"t{i}")
    assert reg.get("trip").snapshot()["state"] == "open"
