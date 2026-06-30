"""Rollout state machine: A/B auto-promote/rollback + max-duration conclude."""

from __future__ import annotations

import random

from app.video.experiments import (
    ExperimentRunner,
    RolloutState,
)
from app.video.experiments.assignment import RenderUnit

from .conftest import FakeClock, two_arm_experiment
from .conftest import feed as _feed


def test_ab_runner_holds_then_promotes_winner(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=50)
    runner = ExperimentRunner(exp, clock=FakeClock(step=1.0))

    # First tick with no data → ramps into HOLDING, no decision.
    d0 = runner.evaluate()
    assert d0.state is RolloutState.HOLDING
    assert d0.report.recommendation.value == "hold"

    _feed(runner, "control", 300, accept_p=0.60, fail_p=0.01, rng=rng)
    _feed(runner, "treat", 300, accept_p=0.85, fail_p=0.01, rng=rng)
    d1 = runner.evaluate()
    assert d1.state is RolloutState.PROMOTED
    assert d1.changed
    assert d1.report.winner_key == "treat"


def test_ab_runner_auto_rollback_on_guardrail(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=50, guardrail_margin=0.10)
    runner = ExperimentRunner(exp, clock=FakeClock(step=1.0))
    _feed(runner, "control", 300, accept_p=0.7, fail_p=0.02, rng=rng)
    _feed(runner, "treat", 300, accept_p=0.7, fail_p=0.45, rng=rng)
    d = runner.evaluate()
    assert d.state is RolloutState.ROLLED_BACK
    assert d.traffic_percent == 0.0
    assert "treat" in d.report.rollback_keys


def test_rollback_pins_traffic_to_control(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=50, guardrail_margin=0.10)
    runner = ExperimentRunner(exp, clock=FakeClock(step=1.0))
    _feed(runner, "control", 300, accept_p=0.7, fail_p=0.02, rng=rng)
    _feed(runner, "treat", 300, accept_p=0.7, fail_p=0.45, rng=rng)
    runner.evaluate()
    # After rollback, every unit falls back to control behavior (not enrolled).
    for i in range(50):
        a = runner.assign(RenderUnit(book_id=f"b{i}"))
        assert not a.in_experiment


def test_terminal_state_is_sticky(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=50)
    runner = ExperimentRunner(exp, clock=FakeClock(step=1.0))
    _feed(runner, "control", 300, accept_p=0.6, fail_p=0.0, rng=rng)
    _feed(runner, "treat", 300, accept_p=0.85, fail_p=0.0, rng=rng)
    d1 = runner.evaluate()
    assert d1.state is RolloutState.PROMOTED
    d2 = runner.evaluate()  # further ticks don't move a terminal runner
    assert d2.state is RolloutState.PROMOTED
    assert not d2.changed


def test_max_duration_concludes_without_decision(rng: random.Random) -> None:
    clock = FakeClock(step=0.0)  # control elapsed manually
    exp = two_arm_experiment(min_samples_per_arm=50, max_duration_s=100.0)
    runner = ExperimentRunner(exp, clock=clock)
    # Inconclusive data (identical arms), plenty of samples.
    _feed(runner, "control", 300, accept_p=0.7, fail_p=0.0, rng=rng)
    _feed(runner, "treat", 300, accept_p=0.7, fail_p=0.0, rng=rng)
    runner.evaluate()  # HOLDING (no winner)
    clock.advance(101.0)  # blow past the budget
    d = runner.evaluate()
    assert d.state is RolloutState.CONCLUDED
    assert "max duration" in d.detail


def test_runner_assignment_honours_target_percent() -> None:
    exp = two_arm_experiment(min_samples_per_arm=50)
    runner = ExperimentRunner(exp, clock=FakeClock(), target_percent=5.0)
    assert runner.traffic_percent == 5.0
    enrolled = sum(
        1 for i in range(2000) if runner.assign(RenderUnit(book_id=f"b{i}")).in_experiment
    )
    # ~5% of 2000 ≈ 100, comfortably under half.
    assert 40 < enrolled < 200


def test_runner_rejects_bad_target_percent() -> None:
    import pytest

    with pytest.raises(ValueError):
        ExperimentRunner(two_arm_experiment(), clock=FakeClock(), target_percent=0.0)
    with pytest.raises(ValueError):
        ExperimentRunner(two_arm_experiment(), clock=FakeClock(), target_percent=150.0)
