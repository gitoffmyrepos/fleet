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
    """Per-upstream sliding-window failure counter with cooldown probe.

    State machine:
        CLOSED → OPEN     when `failure_threshold` failures land inside
                          `window_seconds` (threshold check uses ``>=``)
        OPEN   → HALF_OPEN  on read of `state` after `cooldown_seconds`,
                          persisted on the next `guard()` call
        HALF_OPEN → CLOSED  on `record_success()`
        HALF_OPEN → OPEN    on `record_failure()` (clears the window)

    Caller contract: ``guard()`` before each attempt, then ``record_success()``
    or ``record_failure()`` after the result is known. Use ``snapshot()`` for
    monitoring; ``close()`` for operator-driven manual reset.

    Single-process / single-event-loop only — not thread- or process-safe.
    """

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
            # Persist the lazy OPEN→HALF_OPEN transition computed in `state`.
            # Looks like a no-op but `_state` is still OPEN at this point;
            # writing HALF_OPEN here lets `record_failure`/`record_success`
            # branch on `self._state` directly.
            self._state = State.HALF_OPEN

    def record_failure(self) -> None:
        """Record one failure.

        From HALF_OPEN this re-opens immediately and clears the window so
        the next CLOSED run starts from zero (the probe failure itself is
        NOT counted in the new window). From CLOSED, the failure is added
        to the sliding window; trip when count >= `failure_threshold`.
        """
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
        # `_state == HALF_OPEN` covers the post-`guard()` case;
        # `state == HALF_OPEN` covers OPEN-with-elapsed-cooldown when
        # a caller skipped `guard()` and went straight to record_success.
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
        """Return current state + window-trimmed failure count for monitoring."""
        self._drop_old()
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count_in_window": len(self._failures),
            "opened_at": self._opened_at if self._state == State.OPEN else None,
        }


class CircuitRegistry:
    """Named per-upstream `CircuitBreaker` instances, lazy-created on first `get()`."""

    def __init__(
        self,
        *,
        failure_threshold: int,
        window_seconds: float,
        cooldown_seconds: float,
    ) -> None:
        self._k = failure_threshold
        self._win = window_seconds
        self._cool = cooldown_seconds
        self._items: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        cb = self._items.get(name)
        if cb is None:
            cb = CircuitBreaker(
                name=name,
                failure_threshold=self._k,
                window_seconds=self._win,
                cooldown_seconds=self._cool,
            )
            self._items[name] = cb
        return cb

    def snapshot_all(self) -> list[dict[str, Any]]:
        return [cb.snapshot() for cb in self._items.values()]

    def close(self, name: str) -> bool:
        cb = self._items.get(name)
        if cb is None:
            return False
        cb.close()
        return True
