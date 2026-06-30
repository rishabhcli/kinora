"""Deterministic tests for the deep-health framework (app.slo.health).

Synthetic in-memory probes — no infra, no network. Covers criticality folding,
timeout handling, parallel evaluation, aggregation, and liveness vs readiness.
"""

from __future__ import annotations

import asyncio

import pytest

from app.slo.health import (
    Criticality,
    HealthRegistry,
    HealthStatus,
    ProbeResult,
    aggregate,
)


async def _up() -> ProbeResult:
    return ProbeResult.up("ok")


async def _down() -> ProbeResult:
    return ProbeResult.down("boom")


async def _degraded() -> ProbeResult:
    return ProbeResult.degraded("slow")


async def _raises() -> ProbeResult:
    raise RuntimeError("kaboom")


async def _hang() -> ProbeResult:
    await asyncio.sleep(10.0)
    return ProbeResult.up()


@pytest.mark.asyncio
async def test_all_up_is_ready_and_up() -> None:
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.register("cache", _up, criticality=Criticality.CRITICAL)
    report = await reg.readiness()
    assert report.status is HealthStatus.UP
    assert report.ready is True
    assert {o.name for o in report.outcomes} == {"db", "cache"}


@pytest.mark.asyncio
async def test_critical_down_is_not_ready() -> None:
    reg = HealthRegistry()
    reg.register("db", _down, criticality=Criticality.CRITICAL)
    reg.register("cache", _up, criticality=Criticality.CRITICAL)
    report = await reg.readiness()
    assert report.status is HealthStatus.DOWN
    assert report.ready is False
    assert [o.name for o in report.blocking] == ["db"]


@pytest.mark.asyncio
async def test_optional_down_is_degraded_but_ready() -> None:
    # A non-critical dependency being DOWN must NOT pull the instance out of
    # rotation — the worst *status* surfaces (down) but ready stays True.
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.register("object_store", _down, criticality=Criticality.OPTIONAL)
    report = await reg.readiness()
    assert report.ready is True
    assert report.status is HealthStatus.DOWN  # worst observed status
    assert report.blocking == ()


@pytest.mark.asyncio
async def test_optional_degraded_yields_degraded_aggregate() -> None:
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.register("providers", _degraded, criticality=Criticality.OPTIONAL)
    report = await reg.readiness()
    assert report.ready is True
    assert report.status is HealthStatus.DEGRADED
    assert report.degraded is True


@pytest.mark.asyncio
async def test_critical_degraded_blocks_readiness() -> None:
    # A critical dependency reporting impairment is a readiness concern.
    reg = HealthRegistry()
    reg.register("db", _degraded, criticality=Criticality.CRITICAL)
    report = await reg.readiness()
    assert report.status is HealthStatus.DEGRADED
    assert report.ready is False


@pytest.mark.asyncio
async def test_probe_timeout_is_a_distinct_outcome_and_does_not_hang() -> None:
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.register("slow", _hang, criticality=Criticality.CRITICAL, timeout_s=0.02)
    report = await asyncio.wait_for(reg.readiness(), timeout=2.0)
    slow = next(o for o in report.outcomes if o.name == "slow")
    assert slow.status is HealthStatus.TIMEOUT
    assert report.ready is False  # critical timeout blocks


@pytest.mark.asyncio
async def test_optional_timeout_degrades_but_ready() -> None:
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.register("mcp", _hang, criticality=Criticality.OPTIONAL, timeout_s=0.02)
    report = await asyncio.wait_for(reg.readiness(), timeout=2.0)
    assert report.ready is True
    assert report.status is HealthStatus.TIMEOUT  # worst status


@pytest.mark.asyncio
async def test_probe_exception_becomes_down_never_raises() -> None:
    reg = HealthRegistry()
    reg.register("db", _raises, criticality=Criticality.CRITICAL)
    report = await reg.readiness()
    out = report.outcomes[0]
    assert out.status is HealthStatus.DOWN
    assert "kaboom" in out.result.detail
    assert report.ready is False


@pytest.mark.asyncio
async def test_bare_bool_probe_supported() -> None:
    reg = HealthRegistry()
    reg.register("ping_true", lambda: _true(), criticality=Criticality.CRITICAL)
    reg.register("ping_false", lambda: _false(), criticality=Criticality.OPTIONAL)
    report = await reg.readiness()
    statuses = {o.name: o.status for o in report.outcomes}
    assert statuses["ping_true"] is HealthStatus.UP
    assert statuses["ping_false"] is HealthStatus.DOWN
    assert report.ready is True  # the False one is optional


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False


@pytest.mark.asyncio
async def test_parallel_evaluation_is_max_not_sum() -> None:
    # Two 50ms probes should complete in ~50ms (parallel), well under 90ms.
    async def slow_ok() -> ProbeResult:
        await asyncio.sleep(0.05)
        return ProbeResult.up()

    reg = HealthRegistry()
    reg.register("a", slow_ok, timeout_s=1.0)
    reg.register("b", slow_ok, timeout_s=1.0)
    report = await reg.readiness()
    assert report.ready is True
    assert report.duration_ms < 90.0  # parallel, not 100ms serial


@pytest.mark.asyncio
async def test_liveness_is_independent_of_dependencies() -> None:
    reg = HealthRegistry()
    reg.register("db", _down, criticality=Criticality.CRITICAL)
    # Liveness never runs probes — a dead DB doesn't make the process "dead".
    assert reg.liveness() == {"status": "alive", "live": True}
    reg.live = False
    assert reg.liveness() == {"status": "draining", "live": False}


@pytest.mark.asyncio
async def test_draining_process_is_never_ready_even_when_deps_up() -> None:
    reg = HealthRegistry()
    reg.register("db", _up, criticality=Criticality.CRITICAL)
    reg.live = False
    report = await reg.readiness()
    assert report.ready is False
    assert report.status is HealthStatus.DOWN


@pytest.mark.asyncio
async def test_register_replaces_same_name() -> None:
    reg = HealthRegistry()
    reg.register("db", _down)
    reg.register("db", _up)
    assert len(reg.probes) == 1
    report = await reg.readiness()
    assert report.ready is True


@pytest.mark.asyncio
async def test_empty_registry_is_up_and_ready() -> None:
    report = await HealthRegistry().readiness()
    assert report.ready is True
    assert report.status is HealthStatus.UP


def test_aggregate_pure_function() -> None:
    assert aggregate(()) == (HealthStatus.UP, True)
