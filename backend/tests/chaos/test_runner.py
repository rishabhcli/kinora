"""Deterministic game-day runner tests: steady-state pass, auto-abort + rollback,
preflight, prod-gate refusal, and the findings report (virtual clock, no infra)."""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from app.chaos.clock import VirtualClock
from app.chaos.experiment import AbortConditions, ChaosExperiment, ScheduledFault
from app.chaos.faults import DependencyDownFault, ErrorFault, LatencyFault
from app.chaos.gate import ChaosDisarmedError
from app.chaos.interceptor import FaultInjector
from app.chaos.report import Verdict
from app.chaos.runner import GameDayRunner
from app.chaos.steady_state import (
    SteadyStateHypothesis,
    availability_at_least,
)


@dataclass
class _Settings:
    app_env: str = "local"
    chaos_enabled: bool = True


def _hyp() -> SteadyStateHypothesis:
    return SteadyStateHypothesis.of([availability_at_least(0.95)])


def _exp(**kw: object) -> ChaosExperiment:
    defaults: dict[str, object] = {
        "name": "t",
        "hypothesis": _hyp(),
        "blast_radius": ["dashscope"],
        "schedule": [
            ScheduledFault(ErrorFault(dependency="dashscope", name="boom"), arm_at_s=1.0)
        ],
        "duration_s": 5.0,
        "poll_interval_s": 1.0,
    }
    defaults.update(kw)
    return ChaosExperiment.of(**defaults)  # type: ignore[arg-type]


async def test_steady_state_holds_when_probe_stays_healthy() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    async def probe() -> Mapping[str, float]:
        return {"availability": 0.99}  # always healthy

    report = await runner.run(_exp(), injector=inj, probe=probe)
    assert report.verdict is Verdict.HELD
    assert report.held
    # Faults always rolled back at the end.
    assert inj.armed_dependencies == set()
    # The whole game-day ran on the virtual clock (no real wait).
    assert report.duration_s == pytest.approx(5.0, abs=1.0)


async def test_auto_abort_on_breach_and_rollback() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    calls = {"n": 0}

    async def probe() -> Mapping[str, float]:
        calls["n"] += 1
        # Healthy at preflight + first tick, then breaches.
        return {"availability": 0.99 if calls["n"] <= 2 else 0.50}

    report = await runner.run(_exp(), injector=inj, probe=probe)
    assert report.verdict is Verdict.BREACHED
    assert "availability" in report.breaching_metrics
    assert report.abort_reason is not None and "auto-abort" in report.abort_reason
    # The faults were rolled back the instant the breach was detected.
    assert inj.armed_dependencies == set()


async def test_breach_tolerance_rides_out_transient_blip() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    calls = {"n": 0}

    async def probe() -> Mapping[str, float]:
        calls["n"] += 1
        # One single breaching poll (3rd), healthy otherwise.
        return {"availability": 0.50 if calls["n"] == 3 else 0.99}

    # tolerance=2 → a lone breach does not abort.
    exp = _exp(abort=AbortConditions(breach_tolerance=2))
    report = await runner.run(exp, injector=inj, probe=probe)
    assert report.verdict is Verdict.HELD


async def test_preflight_failure_refuses_to_arm() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    async def probe() -> Mapping[str, float]:
        return {"availability": 0.10}  # already broken before any fault

    report = await runner.run(_exp(), injector=inj, probe=probe)
    assert report.verdict is Verdict.PREFLIGHT_FAILED
    # Exactly one sample (the preflight); no faults were ever armed.
    assert len(report.samples) == 1
    assert inj.armed_dependencies == set()


async def test_prod_gate_refuses_to_run() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(app_env="production", chaos_enabled=True), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    async def probe() -> Mapping[str, float]:
        return {"availability": 0.99}

    with pytest.raises(ChaosDisarmedError):
        await runner.run(_exp(), injector=inj, probe=probe)


async def test_abort_condition_max_errors() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    async def ok() -> str:
        return "x"

    # The probe itself drives faulted dependency calls so injected errors accrue.
    async def probe() -> Mapping[str, float]:
        for _ in range(5):
            with contextlib.suppress(Exception):
                await inj.call("dashscope", ok)
        return {"availability": 0.99}  # steady state stays healthy → no breach

    exp = _exp(
        schedule=[
            ScheduledFault(
                DependencyDownFault(dependency="dashscope", name="down"), arm_at_s=0.0
            )
        ],
        abort=AbortConditions(max_injected_errors=3),
    )
    report = await runner.run(exp, injector=inj, probe=probe)
    assert report.verdict is Verdict.ABORTED
    assert report.abort_reason is not None and "injected errors" in report.abort_reason
    assert inj.armed_dependencies == set()


async def test_findings_report_shape_and_counters() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    async def ok() -> str:
        return "x"

    async def probe() -> Mapping[str, float]:
        # Each poll makes one faulted dashscope call so the timeline is non-empty.
        with contextlib.suppress(Exception):
            await inj.call("dashscope", ok)
        return {"availability": 0.99}

    exp = _exp(
        schedule=[
            ScheduledFault(
                LatencyFault(dependency="dashscope", name="slow", base_latency_s=0.1),
                arm_at_s=0.0,
            )
        ],
    )
    report = await runner.run(exp, injector=inj, probe=probe)
    d = report.to_dict()
    assert d["experiment"] == "t"
    assert d["verdict"] == "held"
    counters = d["counters"]
    assert isinstance(counters, dict)
    assert counters["polls"] >= 1
    assert counters["faults_fired"] >= 1
    assert "dashscope" in counters["affected_dependencies"]
    assert "availability" in report.worst_margins()
    assert report.summary_line().startswith("[chaos] t: HELD")


async def test_blast_radius_scoped_on_injector_by_runner() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=1, clock=clock)

    async def probe() -> Mapping[str, float]:
        return {"availability": 0.99}

    await runner.run(_exp(blast_radius=["dashscope"]), injector=inj, probe=probe)
    # The runner set the injector scope to the experiment's blast radius.
    assert inj.scope == {"dashscope"}
    assert inj.in_scope("dashscope")
    assert not inj.in_scope("redis")
