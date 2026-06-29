"""Tests for active health checking + passive outlier detection."""

from __future__ import annotations

import anyio

from app.distributed.rpc.deadline import ManualClock
from app.distributed.rpc.errors import not_found, unavailable
from app.distributed.rpc.health import (
    HealthCheck,
    HealthChecker,
    HealthStatus,
    OutlierConfig,
    OutlierDetector,
    always_healthy,
)


def _check(name: str, status: HealthStatus, *, critical: bool = True) -> HealthCheck:
    async def probe() -> HealthStatus:
        return status

    return HealthCheck(name=name, probe=probe, critical=critical)


# -- HealthChecker ---------------------------------------------------------- #


async def test_all_healthy_aggregates_healthy() -> None:
    checker = HealthChecker(
        checks=[_check("db", HealthStatus.HEALTHY), _check("redis", HealthStatus.HEALTHY)]
    )
    report = await checker.run()
    assert report.healthy
    assert len(report.components) == 2


async def test_critical_unhealthy_fails_aggregate() -> None:
    checker = HealthChecker(
        checks=[_check("db", HealthStatus.UNHEALTHY, critical=True)]
    )
    report = await checker.run()
    assert report.status is HealthStatus.UNHEALTHY


async def test_noncritical_failure_only_degrades() -> None:
    checker = HealthChecker(
        checks=[
            _check("db", HealthStatus.HEALTHY),
            _check("cache", HealthStatus.UNHEALTHY, critical=False),
        ]
    )
    report = await checker.run()
    assert report.status is HealthStatus.DEGRADED


async def test_raising_probe_is_unhealthy() -> None:
    async def boom() -> HealthStatus:
        raise RuntimeError("pool exhausted")

    checker = HealthChecker(checks=[HealthCheck(name="db", probe=boom)])
    report = await checker.run()
    assert report.status is HealthStatus.UNHEALTHY
    assert "pool exhausted" in (report.components[0].error or "")


async def test_timed_out_probe_is_unhealthy() -> None:
    async def hang() -> HealthStatus:
        await anyio.sleep(10)
        return HealthStatus.HEALTHY

    checker = HealthChecker(checks=[HealthCheck(name="slow", probe=hang, timeout_s=0.01)])
    report = await checker.run()
    assert report.status is HealthStatus.UNHEALTHY
    assert "timed out" in (report.components[0].error or "")


async def test_empty_checker_is_healthy() -> None:
    report = await HealthChecker().run()
    assert report.healthy


async def test_always_healthy_probe() -> None:
    checker = HealthChecker(checks=[HealthCheck(name="x", probe=always_healthy())])
    report = await checker.run()
    assert report.healthy
    assert report.to_dict()["status"] == "healthy"


# -- OutlierDetector -------------------------------------------------------- #


def test_ejects_after_consecutive_transport_failures() -> None:
    clk = ManualClock()
    det = OutlierDetector(
        config=OutlierConfig(consecutive_failures=3, ejection_base_s=5.0), clock=clk
    )
    for _ in range(2):
        det.record("a", unavailable("down"))
    assert not det.is_ejected("a")
    det.record("a", unavailable("down"))  # 3rd → eject
    assert det.is_ejected("a")
    assert "a" in det.ejected_instances()


def test_application_errors_do_not_eject() -> None:
    clk = ManualClock()
    det = OutlierDetector(config=OutlierConfig(consecutive_failures=2), clock=clk)
    det.record("a", not_found("absent"))
    det.record("a", not_found("absent"))
    assert not det.is_ejected("a")


def test_ejection_expires_after_cooldown() -> None:
    clk = ManualClock()
    det = OutlierDetector(
        config=OutlierConfig(consecutive_failures=1, ejection_base_s=5.0), clock=clk
    )
    det.record("a", unavailable("down"))
    assert det.is_ejected("a")
    clk.advance(5.1)
    assert not det.is_ejected("a")


def test_successes_reinstate() -> None:
    clk = ManualClock()
    det = OutlierDetector(
        config=OutlierConfig(consecutive_failures=1, success_reinstate=2, ejection_base_s=100.0),
        clock=clk,
    )
    det.record("a", unavailable("down"))
    assert det.is_ejected("a")
    det.record("a", None)
    det.record("a", None)  # 2 successes reinstate even before cooldown
    assert not det.is_ejected("a")


def test_ejection_backoff_grows() -> None:
    clk = ManualClock()
    det = OutlierDetector(
        config=OutlierConfig(consecutive_failures=1, ejection_base_s=5.0, max_ejection_s=100.0),
        clock=clk,
    )
    det.record("a", unavailable("x"))  # eject ~5s
    clk.advance(5.1)
    assert not det.is_ejected("a")
    det.record("a", unavailable("x"))  # 2nd ejection ~10s
    clk.advance(5.1)
    assert det.is_ejected("a")  # still ejected (10s window)
    clk.advance(5.0)
    assert not det.is_ejected("a")
