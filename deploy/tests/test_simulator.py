"""Tests for the deterministic, cloud-free deployment simulator (§12.6).

These assert the canonical scenarios behave correctly and that the whole
simulation is reproducible with no cloud and no real time.
"""

from __future__ import annotations

import pytest

from deploy.orchestrator.drain import DrainPhase
from deploy.orchestrator.models import DeployState
from deploy.orchestrator.simulator import (
    ALL_SCENARIOS,
    SimReport,
    main,
    scenario_happy_blue_green,
    scenario_happy_canary,
    scenario_health_fail,
    scenario_live_video_blocked,
    scenario_slo_breach,
    scenario_smoke_fail,
    scenario_stuck_drain,
    simulate,
)


async def test_happy_canary_succeeds() -> None:
    report = await simulate(scenario_happy_canary())
    assert report.succeeded
    assert report.final_state is DeployState.SUCCEEDED
    assert report.max_weight_reached == 1.0
    assert report.torn_down == []  # nothing torn down on success


async def test_happy_blue_green_succeeds() -> None:
    report = await simulate(scenario_happy_blue_green())
    assert report.succeeded
    assert report.max_weight_reached == 1.0


async def test_slo_breach_auto_rolls_back() -> None:
    report = await simulate(scenario_slo_breach())
    assert report.rolled_back
    assert report.max_weight_reached == 0.05  # blast radius limited to the canary
    assert report.torn_down  # the bad fleet was retired
    assert "SLO breach" in report.result.reason


async def test_health_fail_rolls_back_with_no_traffic() -> None:
    report = await simulate(scenario_health_fail())
    assert report.rolled_back
    assert report.max_weight_reached == 0.0  # never shifted real traffic


async def test_smoke_fail_rolls_back() -> None:
    report = await simulate(scenario_smoke_fail())
    assert report.rolled_back
    assert "smoke" in report.result.reason


async def test_stuck_drain_releases_jobs_but_still_succeeds() -> None:
    report = await simulate(scenario_stuck_drain())
    assert report.succeeded
    assert report.drain is not None
    assert report.drain.phase is DrainPhase.TIMED_OUT
    assert report.drain.released == 3  # the wedged jobs returned to the queue


async def test_live_video_blocked_fails_before_provision() -> None:
    report = await simulate(scenario_live_video_blocked())
    assert report.final_state is DeployState.FAILED
    assert report.provisioned == []
    assert "KINORA_LIVE_VIDEO" in report.result.reason


async def test_simulation_is_deterministic() -> None:
    a = await simulate(scenario_slo_breach())
    b = await simulate(scenario_slo_breach())
    assert a.final_state is b.final_state
    assert a.transcript() == b.transcript()
    assert a.router_history == b.router_history


async def test_all_named_scenarios_run() -> None:
    for name, factory in ALL_SCENARIOS.items():
        report = await simulate(factory())
        assert isinstance(report, SimReport)
        assert report.final_state in {
            DeployState.SUCCEEDED,
            DeployState.ROLLED_BACK,
            DeployState.FAILED,
        }, name


def test_main_runs_all_scenarios_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--scenario", "all"])
    assert rc == 0
    out = capsys.readouterr().out
    # Each scenario prints its header.
    for name in ALL_SCENARIOS:
        assert f"scenario: {name}" in out


def test_main_single_scenario(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["--scenario", "slo-breach"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rolled-back" in out


async def test_transcript_records_full_rollback_story() -> None:
    report = await simulate(scenario_slo_breach())
    transcript = report.transcript()
    # The audit trail should narrate the whole decision.
    assert "plan" in transcript
    assert "BREACH" in transcript
    assert "rollback" in transcript
    assert "rolled_back" in transcript
