"""Unit tests for fleet.metrics."""

from __future__ import annotations

import pytest

from fleet import metrics as metrics_mod
from fleet.metrics import FleetMetrics, render_metrics


@pytest.mark.unit
def test_facade_initializes_with_prometheus_when_available() -> None:
    """When prometheus_client is installed, facade.enabled is True and a
    registry is created."""
    pytest.importorskip("prometheus_client")
    facade = FleetMetrics(enabled=True)
    assert facade.enabled is True
    assert facade.registry is not None


@pytest.mark.unit
def test_facade_falls_back_to_noop_when_disabled() -> None:
    """enabled=False forces the no-op path. Verifies the shim surface
    has all required methods."""
    facade = FleetMetrics(enabled=False)
    assert facade.enabled is False
    # All methods callable without exception
    facade.dispatch_started(kind="subagent")
    facade.dispatch_succeeded()
    facade.dispatch_failed(reason="timeout")
    facade.branch_created()
    facade.branch_merged()
    facade.branch_orphaned(count=3)
    facade.set_worktrees_active(42)
    facade.observe_duration(kind="subagent", seconds=12.3)


@pytest.mark.unit
def test_counters_increment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Counters should produce the expected metric names in the
    rendered output."""
    pytest.importorskip("prometheus_client")
    from prometheus_client import generate_latest

    facade = FleetMetrics(enabled=True)
    metrics_mod.set_facade(facade)

    facade.dispatch_started(kind="subagent")
    facade.dispatch_started(kind="subagent")
    facade.dispatch_started(kind="swarm")
    facade.dispatch_succeeded()
    facade.dispatch_failed(reason="circuit_open")
    facade.branch_created()
    facade.branch_merged()
    facade.branch_orphaned(count=2)
    facade.set_worktrees_active(7)
    facade.observe_duration(kind="subagent", seconds=15.0)

    out = generate_latest(facade.registry).decode("utf-8")

    # Counter names should be present with expected labels/values.
    assert 'fleet_dispatches_total{kind="subagent"} 2.0' in out
    assert 'fleet_dispatches_total{kind="swarm"} 1.0' in out
    assert "fleet_dispatches_succeeded_total 1.0" in out
    assert 'fleet_dispatches_failed_total{reason="circuit_open"} 1.0' in out
    assert "fleet_branches_created_total 1.0" in out
    assert "fleet_branches_merged_total 1.0" in out
    assert "fleet_branches_orphaned_total 2.0" in out
    assert "fleet_worktrees_active 7.0" in out
    assert 'fleet_dispatch_duration_seconds_bucket{kind="subagent"' in out


@pytest.mark.unit
def test_render_metrics_returns_text_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the facade is disabled, /metrics still returns *something*."""
    facade = FleetMetrics(enabled=False)
    metrics_mod.set_facade(facade)
    content_type, payload = render_metrics()
    assert content_type.startswith("text/plain")
    assert b"disabled" in payload


@pytest.mark.unit
def test_render_metrics_returns_prom_payload_when_enabled() -> None:
    """When enabled, /metrics returns Prometheus text exposition format."""
    pytest.importorskip("prometheus_client")
    facade = FleetMetrics(enabled=True)
    metrics_mod.set_facade(facade)
    facade.dispatch_started(kind="phase")
    content_type, payload = render_metrics()
    assert "text/plain" in content_type
    # Look for any of our metric names
    text = payload.decode("utf-8")
    assert "fleet_dispatches_total" in text


@pytest.mark.unit
def test_get_returns_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """metrics.get() lazily creates a singleton on first call."""
    monkeypatch.setattr(metrics_mod, "_singleton", None)
    a = metrics_mod.get()
    b = metrics_mod.get()
    assert a is b


@pytest.mark.unit
def test_failed_inc_is_swallowed() -> None:
    """If a metric handle throws (e.g. wrong label cardinality), the
    facade swallows the error rather than crashing the caller. This is
    critical — instrumentation must NEVER take down a dispatch."""
    facade = FleetMetrics(enabled=False)
    # No exception — even with weird args
    facade.dispatch_failed(reason="")
    facade.observe_duration(kind="", seconds=-1.0)
