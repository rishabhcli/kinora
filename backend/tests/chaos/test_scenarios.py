"""Tests for the named Kinora game-day scenario catalogue (no infra)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pytest

from app.chaos import scenarios
from app.chaos.clock import VirtualClock
from app.chaos.experiment import ChaosExperiment
from app.chaos.interceptor import FaultInjector
from app.chaos.report import Verdict
from app.chaos.runner import GameDayRunner


@dataclass
class _Settings:
    app_env: str = "local"
    chaos_enabled: bool = True


@pytest.mark.parametrize("name,builder", sorted(scenarios.CATALOGUE.items()))
def test_every_scenario_validates(name: str, builder) -> None:  # type: ignore[no-untyped-def]
    exp = builder()
    assert isinstance(exp, ChaosExperiment)
    assert exp.name == name
    # Every scheduled fault is within the declared blast radius (construction
    # already asserts this, so building is the test).
    for entry in exp.schedule:
        assert entry.fault.dependency in exp.blast_radius


def test_scenarios_are_seedable_and_deterministic() -> None:
    a = scenarios.provider_rate_limit_storm(seed=7)
    b = scenarios.provider_rate_limit_storm(seed=7)
    assert a.seed == b.seed == 7
    assert a.name == b.name


async def test_provider_storm_scenario_holds_under_healthy_probe() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=7, clock=clock)
    exp = scenarios.provider_rate_limit_storm(seed=7)

    async def probe() -> Mapping[str, float]:
        # System under test degrades gracefully → stays inside the steady state.
        return {"availability": 0.99, "error_rate": 0.01}

    report = await runner.run(exp, injector=inj, probe=probe)
    assert report.verdict is Verdict.HELD


async def test_redis_outage_scenario_auto_aborts_when_system_falls_over() -> None:
    clock = VirtualClock(start=0.0)
    runner = GameDayRunner(_Settings(), clock=clock)
    inj = FaultInjector(seed=7, clock=clock)
    exp = scenarios.redis_outage(seed=7)

    polls = {"n": 0}

    async def probe() -> Mapping[str, float]:
        polls["n"] += 1
        # Healthy through preflight, then the outage tanks availability.
        if polls["n"] <= 4:
            return {"availability": 0.99, "error_rate": 0.0}
        return {"availability": 0.40, "error_rate": 0.60}

    report = await runner.run(exp, injector=inj, probe=probe)
    assert report.verdict is Verdict.BREACHED
    assert inj.armed_dependencies == set()  # rolled back
