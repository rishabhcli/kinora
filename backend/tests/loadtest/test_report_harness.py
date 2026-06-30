"""Report rendering (text + JSON) and the one-call harness facade."""

from __future__ import annotations

import json

import pytest

from app.loadtest.budget import EndpointBudget, LatencyBudget, default_kinora_budget
from app.loadtest.clock import VirtualClock
from app.loadtest.config import LoadtestSettings
from app.loadtest.generator import LoadModel, LoadPlan
from app.loadtest.harness import HarnessResult, run_load_test
from app.loadtest.regression import Baseline
from app.loadtest.scenario import ReadEndpoint, steady_reader
from app.loadtest.target import FakeTarget, constant_service_time
from tests.loadtest.conftest import drive


def _run(
    clock: VirtualClock,
    *,
    latency_s: float = 0.05,
    budget: LatencyBudget | None = None,
    baseline: Baseline | None = None,
) -> HarnessResult:
    target = FakeTarget(clock, constant_service_time(latency_s), seed=1)
    plan = LoadPlan(
        model=LoadModel.CLOSED, scenario=steady_reader(pages=3), users=4, iterations=1, seed=7
    )

    async def go() -> HarnessResult:
        return await run_load_test(plan, target, clock=clock, budget=budget, baseline=baseline)

    return drive(clock, go)


def test_report_text_has_per_endpoint_table() -> None:
    clock = VirtualClock()
    res = _run(clock, budget=default_kinora_budget())
    text = res.report.to_text()
    assert "Kinora load run" in text
    assert "page_turn" in text
    assert "GATE: PASS" in text
    assert "ALL" in text


def test_report_json_round_trips_and_is_valid() -> None:
    clock = VirtualClock()
    res = _run(clock, budget=default_kinora_budget())
    blob = res.report.to_json()
    parsed = json.loads(blob)
    assert parsed["model"] == "closed"
    assert parsed["scenario"] == "steady_reader"
    assert "page_turn" in parsed["per_endpoint"]
    assert parsed["gate"]["passed"] is True


def test_harness_gate_fails_when_target_too_slow() -> None:
    clock = VirtualClock()
    budget = LatencyBudget(
        endpoints={ReadEndpoint.PAGE_TURN: EndpointBudget(ReadEndpoint.PAGE_TURN, {"p95": 0.1})}
    )
    res = _run(clock, latency_s=0.5, budget=budget)  # 500 ms blows a 100 ms p95
    assert res.gate is not None and not res.gate.passed
    assert not res.passed


def test_harness_detects_regression_against_baseline() -> None:
    # Capture a fast baseline.
    clock1 = VirtualClock()
    fast = _run(clock1)
    baseline = Baseline.from_collector(fast.run.collector, label="good")

    # A slow run vs that baseline.
    clock2 = VirtualClock()
    slow = _run(clock2, latency_s=0.3, baseline=baseline)
    assert slow.regression is not None and slow.regression.regressed
    assert not slow.passed


def test_harness_passed_when_clean() -> None:
    clock = VirtualClock()
    res = _run(clock, budget=default_kinora_budget())
    assert res.passed


def test_loadtest_settings_production_guard() -> None:
    s = LoadtestSettings(allow_production=False)
    s.guard_target("http://localhost:8000")  # ok
    with pytest.raises(RuntimeError):
        s.guard_target("https://api.production.kinora.example")
    # Override allows it.
    s2 = LoadtestSettings(allow_production=True)
    s2.guard_target("https://api.production.kinora.example")


def test_report_carries_service_and_corrected_latency() -> None:
    clock = VirtualClock()
    res = _run(clock)
    d = res.report.to_dict()
    pt = d["per_endpoint"]["page_turn"]  # type: ignore[index]
    assert "latency_ms" in pt
    assert "service_latency_ms" in pt
