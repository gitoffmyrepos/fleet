"""Per-upstream circuit breaker."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from enum import StrEnum
from typing import Any


class State(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpen(RuntimeError):
    def __init__(self, name: str, retry_after_seconds: float) -> None:
        super().__init__(f"circuit '{name}' open; retry in {retry_after_seconds:.0f}s")
        self.name = name
        self.retry_after_seconds = retry_after_seconds


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str,
        failure_threshold: int,
        window_seconds: float,
        cooldown_seconds: float,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self._k = failure_threshold
        self._win = window_seconds
        self._cool = cooldown_seconds
        self._now = now
        self._failures: deque[float] = deque()
        self._state: State = State.CLOSED
        self._opened_at: float = 0.0

    @property
    def state(self) -> State:
        if self._state == State.OPEN and self._now() - self._opened_at >= self._cool:
            return State.HALF_OPEN
        return self._state

    def _drop_old(self) -> None:
        cutoff = self._now() - self._win
        while self._failures and self._failures[0] < cutoff:
            self._failures.popleft()

    def guard(self) -> None:
        s = self.state
        if s == State.OPEN:
            retry = self._cool - (self._now() - self._opened_at)
            raise CircuitOpen(self.name, max(retry, 0.0))
        if s == State.HALF_OPEN:
            self._state = State.HALF_OPEN

    def record_failure(self) -> None:
        if self._state == State.HALF_OPEN:
            self._opened_at = self._now()
            self._state = State.OPEN
            self._failures.clear()
            return
        self._failures.append(self._now())
        self._drop_old()
        if len(self._failures) >= self._k:
            self._opened_at = self._now()
            self._state = State.OPEN

    def record_success(self) -> None:
        if self._state == State.HALF_OPEN or self.state == State.HALF_OPEN:
            self._state = State.CLOSED
            self._failures.clear()
            self._opened_at = 0.0
        elif self._state == State.CLOSED:
            self._drop_old()

    def close(self) -> None:
        self._state = State.CLOSED
        self._failures.clear()
        self._opened_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count_in_window": len(self._failures),
            "opened_at": self._opened_at if self._state == State.OPEN else None,
        }
