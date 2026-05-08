import pytest

from fleet.circuit import CircuitBreaker, CircuitOpen, State


def make(monkey_now):
    return CircuitBreaker(
        name="ruflo",
        failure_threshold=3,
        window_seconds=600,
        cooldown_seconds=300,
        now=monkey_now,
    )


def test_initial_state_closed() -> None:
    cb = make(lambda: 0.0)
    assert cb.state == State.CLOSED


def test_failures_below_threshold_stay_closed() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    cb.record_failure()
    cb.record_failure()
    assert cb.state == State.CLOSED


def test_three_failures_in_window_trips() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    cb.record_failure()
    t[0] += 10
    cb.record_failure()
    t[0] += 10
    cb.record_failure()
    assert cb.state == State.OPEN


def test_old_failures_drop_out_of_window() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    cb.record_failure()
    t[0] += 700  # outside 600s window
    cb.record_failure()
    t[0] += 10
    cb.record_failure()
    assert cb.state == State.CLOSED


def test_open_blocks_calls() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    for _ in range(3):
        cb.record_failure()
        t[0] += 1
    with pytest.raises(CircuitOpen) as exc:
        cb.guard()
    assert exc.value.retry_after_seconds > 0


def test_half_open_after_cooldown() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    for _ in range(3):
        cb.record_failure()
        t[0] += 1
    t[0] += 301  # past 300s cooldown
    cb.guard()  # should not raise: probe allowed
    assert cb.state == State.HALF_OPEN


def test_half_open_success_closes() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    for _ in range(3):
        cb.record_failure()
        t[0] += 1
    t[0] += 301
    cb.guard()
    cb.record_success()
    assert cb.state == State.CLOSED


def test_half_open_failure_re_trips() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    for _ in range(3):
        cb.record_failure()
        t[0] += 1
    t[0] += 301
    cb.guard()
    cb.record_failure()
    assert cb.state == State.OPEN


def test_manual_close_resets() -> None:
    t = [0.0]
    cb = make(lambda: t[0])
    for _ in range(3):
        cb.record_failure()
        t[0] += 1
    cb.close()
    assert cb.state == State.CLOSED
    cb.guard()  # no raise
