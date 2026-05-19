"""Prometheus metrics for Fleet MCP.

A2 cleanup deliverable (2026-05-19) — defense-in-depth observability for
the per-dispatch branch lifecycle that Agent A1 introduces. The metrics
expose:

* Dispatch counters (per kind, success/failure) so we can graph swarm
  vs subagent throughput and reason-tag failure patterns.
* Branch lifecycle counters (created/merged/orphaned) — together with
  ``fleet_worktrees_active`` (gauge), the operator can see at a glance
  whether the per-dispatch teardown is keeping up.
* ``fleet_dispatch_duration_seconds`` histogram for wall-clock SLOs.

Implementation notes
--------------------

* ``prometheus_client`` is imported lazily so unit tests don't have to
  install it. When unavailable we fall back to a no-op shim that
  preserves the call surface (``inc()``, ``set()``, ``observe()``,
  ``labels()``); the ``/metrics`` endpoint then returns a 503-style
  comment-only payload.

* Metrics live on a dedicated registry so we never trip the
  ``ValueError: Duplicated timeseries`` failure mode when ``build_app``
  is invoked more than once (which happens repeatedly in the test
  suite). Callers retrieve the registry via :func:`get_registry` and
  hand it to ``prometheus_client.generate_latest``.

* The module exposes a ``FleetMetrics`` facade — a tiny indirection
  that lets non-instrumentation code call ``metrics.dispatch_started(
  kind="subagent")`` without having to know whether prometheus_client
  is installed.

This file is conflict-safe vs A1: A1 only touches the dispatcher; this
module is brand-new and is wired into ``server.py`` from the
Prometheus-metrics block (a separate section from A1's tool
registration).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class _Counter(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...
    def labels(self, **kwargs: str) -> _Counter: ...


class _Gauge(Protocol):
    def inc(self, amount: float = 1.0) -> None: ...
    def dec(self, amount: float = 1.0) -> None: ...
    def set(self, value: float) -> None: ...
    def labels(self, **kwargs: str) -> _Gauge: ...


class _Histogram(Protocol):
    def observe(self, value: float) -> None: ...
    def labels(self, **kwargs: str) -> _Histogram: ...


# ─── No-op shims for environments without prometheus_client ─────────────────


class _NoopMetric:
    """Lightweight stand-in when prometheus_client is unavailable.

    Every method silently no-ops; ``labels()`` returns ``self`` so chained
    calls work. Keeps the instrumentation surface identical between dev
    and production deployments.
    """

    def inc(self, amount: float = 1.0) -> None:
        return None

    def dec(self, amount: float = 1.0) -> None:
        return None

    def set(self, value: float) -> None:
        return None

    def observe(self, value: float) -> None:
        return None

    def labels(self, **kwargs: str) -> _NoopMetric:
        return self


class FleetMetrics:
    """Facade for Fleet's Prometheus metrics.

    Instantiate once at app build time (``build_app``) and reuse for the
    process lifetime. Avoid creating multiple instances — each one
    registers fresh collectors with prometheus_client and double-counts
    metrics.
    """

    def __init__(self, *, enabled: bool = True) -> None:
        self._enabled = enabled
        self._registry: Any | None = None
        # Metric handles are typed `Any` so we can assign either a
        # prometheus_client.Counter/Gauge/Histogram or a _NoopMetric
        # shim depending on whether prometheus_client is installed.
        self.dispatches_total: Any
        self.dispatches_succeeded_total: Any
        self.dispatches_failed_total: Any
        self.branches_created_total: Any
        self.branches_merged_total: Any
        self.branches_orphaned_total: Any
        self.worktrees_active: Any
        self.dispatch_duration_seconds: Any
        # Instantiate metric handles. Real prometheus_client when
        # available, no-op shims otherwise.
        if enabled:
            try:
                from prometheus_client import (
                    CollectorRegistry,
                    Counter,
                    Gauge,
                    Histogram,
                )

                self._registry = CollectorRegistry()
                # Per-kind dispatch counter
                self.dispatches_total = Counter(
                    "fleet_dispatches_total",
                    "Total Fleet dispatches issued, partitioned by kind.",
                    ["kind"],
                    registry=self._registry,
                )
                self.dispatches_succeeded_total = Counter(
                    "fleet_dispatches_succeeded_total",
                    "Total Fleet dispatches that returned ok=True.",
                    registry=self._registry,
                )
                self.dispatches_failed_total = Counter(
                    "fleet_dispatches_failed_total",
                    "Total Fleet dispatches that returned ok=False.",
                    ["reason"],
                    registry=self._registry,
                )
                self.branches_created_total = Counter(
                    "fleet_branches_created_total",
                    "Total Fleet-managed feature branches created by the "
                    "per-dispatch lifecycle (A1).",
                    registry=self._registry,
                )
                self.branches_merged_total = Counter(
                    "fleet_branches_merged_total",
                    "Total Fleet-managed feature branches successfully merged back to master.",
                    registry=self._registry,
                )
                self.branches_orphaned_total = Counter(
                    "fleet_branches_orphaned_total",
                    "Total Fleet-managed feature branches identified as "
                    "orphans by the reconciler (state lost or stale).",
                    registry=self._registry,
                )
                self.worktrees_active = Gauge(
                    "fleet_worktrees_active",
                    "Current count of Fleet-managed worktrees on disk.",
                    registry=self._registry,
                )
                self.dispatch_duration_seconds = Histogram(
                    "fleet_dispatch_duration_seconds",
                    "Wall-clock duration of a Fleet dispatch, end to end.",
                    # Buckets chosen for typical homelab dispatches: a
                    # few-second cache hit, a 30s subagent, a 5min
                    # phase, a 30-60min hive-mind swarm.
                    buckets=(
                        1.0,
                        5.0,
                        15.0,
                        30.0,
                        60.0,
                        120.0,
                        300.0,
                        600.0,
                        1200.0,
                        1800.0,
                        3600.0,
                        7200.0,
                    ),
                    labelnames=["kind"],
                    registry=self._registry,
                )
                return
            except ImportError:
                logger.warning(
                    "prometheus_client not installed; Fleet metrics will "
                    "fall back to no-op shims. Install with "
                    "`uv add prometheus-client` to enable /metrics."
                )

        # No-op fallback path.
        self._enabled = False
        shim = _NoopMetric()
        self.dispatches_total = shim
        self.dispatches_succeeded_total = shim
        self.dispatches_failed_total = shim
        self.branches_created_total = shim
        self.branches_merged_total = shim
        self.branches_orphaned_total = shim
        self.worktrees_active = shim
        self.dispatch_duration_seconds = shim

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def registry(self) -> Any | None:
        return self._registry

    # ─── Convenience helpers (one-line call sites) ──────────────────────

    def dispatch_started(self, *, kind: str) -> None:
        try:
            self.dispatches_total.labels(kind=kind).inc()
        except Exception as e:
            logger.debug("metrics.dispatch_started failed: %s", e)

    def dispatch_succeeded(self) -> None:
        try:
            self.dispatches_succeeded_total.inc()
        except Exception as e:
            logger.debug("metrics.dispatch_succeeded failed: %s", e)

    def dispatch_failed(self, *, reason: str) -> None:
        try:
            self.dispatches_failed_total.labels(reason=reason).inc()
        except Exception as e:
            logger.debug("metrics.dispatch_failed failed: %s", e)

    def branch_created(self) -> None:
        try:
            self.branches_created_total.inc()
        except Exception as e:
            logger.debug("metrics.branch_created failed: %s", e)

    def branch_merged(self) -> None:
        try:
            self.branches_merged_total.inc()
        except Exception as e:
            logger.debug("metrics.branch_merged failed: %s", e)

    def branch_orphaned(self, *, count: int = 1) -> None:
        try:
            self.branches_orphaned_total.inc(count)
        except Exception as e:
            logger.debug("metrics.branch_orphaned failed: %s", e)

    def set_worktrees_active(self, count: int) -> None:
        try:
            self.worktrees_active.set(count)
        except Exception as e:
            logger.debug("metrics.set_worktrees_active failed: %s", e)

    def observe_duration(self, *, kind: str, seconds: float) -> None:
        try:
            self.dispatch_duration_seconds.labels(kind=kind).observe(seconds)
        except Exception as e:
            logger.debug("metrics.observe_duration failed: %s", e)


# Process-wide singleton; ``server.build_app`` populates it.
_singleton: FleetMetrics | None = None


def get() -> FleetMetrics:
    """Return the process-wide metrics facade (lazily instantiated).

    Call sites that don't go through ``build_app`` (unit tests, the
    reconciler script, ad-hoc scripts) still get a working facade so
    instrumentation calls don't have to be guarded.
    """
    global _singleton
    if _singleton is None:
        _singleton = FleetMetrics()
    return _singleton


def set_facade(facade: FleetMetrics) -> None:
    """Replace the process-wide facade (used by ``build_app`` and tests)."""
    global _singleton
    _singleton = facade


def render_metrics() -> tuple[str, bytes]:
    """Return ``(content_type, payload_bytes)`` for the /metrics endpoint.

    Falls back to a single-line text comment when prometheus_client is
    unavailable, so the endpoint always responds.
    """
    facade = get()
    if not facade.enabled or facade.registry is None:
        return (
            "text/plain; version=0.0.4; charset=utf-8",
            b"# fleet metrics disabled (prometheus_client not installed)\n",
        )
    try:
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

        return CONTENT_TYPE_LATEST, generate_latest(facade.registry)
    except Exception as e:
        logger.warning("render_metrics failed: %s", e)
        return (
            "text/plain; version=0.0.4; charset=utf-8",
            f"# fleet metrics render error: {type(e).__name__}\n".encode(),
        )
