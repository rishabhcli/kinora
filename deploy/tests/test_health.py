"""Tests for health probing + stability windows (offline)."""

from __future__ import annotations

import pytest

from deploy.orchestrator.fakes import ScriptedHealthProbe
from deploy.orchestrator.health import (
    HealthGate,
    ProbeResult,
    StabilityWindow,
    quorum_status,
)
from deploy.orchestrator.models import HealthStatus


def test_probe_result_constructors() -> None:
    ok = ProbeResult.ok(postgres=True, redis=True)
    assert ok.healthy is True
    assert ok.checks == {"postgres": True, "redis": True}

    down = ProbeResult.down("redis down", postgres=True, redis=False)
    assert down.healthy is False
    assert down.detail == "redis down"


def test_probe_result_ok_default_checks() -> None:
    assert ProbeResult.ok().checks == {"ready": True}


def test_stability_window_requires_consecutive_healthy() -> None:
    win = StabilityWindow(required=3, max_samples=10)
    win.observe(ProbeResult.ok())
    win.observe(ProbeResult.ok())
    assert not win.is_stable
    win.observe(ProbeResult.ok())
    assert win.is_stable


def test_stability_window_resets_streak_on_unhealthy() -> None:
    win = StabilityWindow(required=2, max_samples=10)
    win.observe(ProbeResult.ok())
    win.observe(ProbeResult.down())  # resets streak
    assert win.streak == 0
    win.observe(ProbeResult.ok())
    assert not win.is_stable
    win.observe(ProbeResult.ok())
    assert win.is_stable


def test_stability_window_exhausts() -> None:
    win = StabilityWindow(required=3, max_samples=4)
    for _ in range(4):
        win.observe(ProbeResult.down())
    assert win.is_exhausted
    assert not win.is_stable


def test_stability_window_validation() -> None:
    with pytest.raises(ValueError):
        StabilityWindow(required=0)
    with pytest.raises(ValueError):
        StabilityWindow(required=5, max_samples=2)


async def test_health_gate_reaches_stable() -> None:
    gate = HealthGate(ScriptedHealthProbe.always_healthy(), window=StabilityWindow(required=3))
    assert await gate.wait_until_stable("slot-1") is True
    assert gate.window.streak >= 3


async def test_health_gate_fails_when_unhealthy() -> None:
    gate = HealthGate(
        ScriptedHealthProbe.always_unhealthy(), window=StabilityWindow(required=3, max_samples=5)
    )
    assert await gate.wait_until_stable("slot-1") is False


async def test_health_gate_handles_flap_then_death() -> None:
    # 2 healthy then dead forever; required=3 → never stabilises.
    gate = HealthGate(
        ScriptedHealthProbe.healthy_then_dead(2),
        window=StabilityWindow(required=3, max_samples=6),
    )
    assert await gate.wait_until_stable("slot-1") is False


def test_quorum_status() -> None:
    healthy = ProbeResult.ok()
    down = ProbeResult.down()
    assert quorum_status([], min_healthy=1.0) is HealthStatus.UNKNOWN
    assert quorum_status([healthy, healthy], min_healthy=1.0) is HealthStatus.HEALTHY
    assert quorum_status([healthy, down], min_healthy=1.0) is HealthStatus.UNHEALTHY
    assert quorum_status([healthy, down], min_healthy=0.5) is HealthStatus.HEALTHY
