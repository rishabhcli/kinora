"""Progressive canary: ramp the ladder, halt on regression, promote when clean."""

from __future__ import annotations

import random

import pytest

from app.video.experiments import (
    DEFAULT_CANARY_LADDER,
    CanaryRunner,
    RolloutState,
)
from app.video.experiments.assignment import RenderUnit

from .conftest import FakeClock, two_arm_experiment
from .conftest import feed as _feed


def _both(
    runner: CanaryRunner, n: int, *, accept_p: float, fail_p: float, rng: random.Random
) -> None:
    _feed(runner, "control", n, accept_p=accept_p, fail_p=fail_p, rng=rng)
    _feed(runner, "treat", n, accept_p=accept_p, fail_p=fail_p, rng=rng)


def test_canary_starts_at_first_rung() -> None:
    exp = two_arm_experiment(min_samples_per_arm=20)
    canary = CanaryRunner(exp, clock=FakeClock(step=1.0))
    assert canary.traffic_percent == DEFAULT_CANARY_LADDER[0] == 1.0
    assert canary.rung_index == 0
    assert canary.state is RolloutState.RAMPING


def test_canary_climbs_ladder_when_clean(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=20)
    canary = CanaryRunner(exp, clock=FakeClock(step=1.0), min_samples_per_rung=20)
    seen: list[float] = [canary.traffic_percent]
    for _ in range(6):
        _both(canary, 25, accept_p=0.75, fail_p=0.0, rng=rng)
        d = canary.evaluate()
        seen.append(d.traffic_percent)
        if d.state.is_terminal:
            break
    # The ladder was climbed in order, then promoted at 100%.
    assert seen[:5] == [1.0, 5.0, 25.0, 100.0, 100.0]
    assert canary.state is RolloutState.PROMOTED


def test_canary_halts_on_regression_mid_ladder(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=20, guardrail_margin=0.10)
    canary = CanaryRunner(exp, clock=FakeClock(step=1.0), min_samples_per_rung=20)

    # rung 0 (1%) clean → advance to 5%.
    _both(canary, 25, accept_p=0.8, fail_p=0.0, rng=rng)
    d1 = canary.evaluate()
    assert d1.traffic_percent == 5.0
    assert canary.state is RolloutState.RAMPING

    # now the treatment regresses badly at the higher rung → halt.
    _feed(canary, "control", 60, accept_p=0.8, fail_p=0.02, rng=rng)
    _feed(canary, "treat", 60, accept_p=0.8, fail_p=0.5, rng=rng)
    d2 = canary.evaluate()
    assert d2.state is RolloutState.ROLLED_BACK
    assert d2.traffic_percent == 0.0
    assert "treat" in d2.report.rollback_keys


def test_canary_does_not_advance_without_enough_data(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=100)
    canary = CanaryRunner(exp, clock=FakeClock(step=1.0), min_samples_per_rung=100)
    _both(canary, 10, accept_p=0.8, fail_p=0.0, rng=rng)  # well under the floor
    d = canary.evaluate()
    assert d.traffic_percent == 1.0  # stayed on the first rung
    assert canary.rung_index == 0
    assert not d.changed


def test_canary_promotes_on_clean_top_rung_even_without_significant_win(rng: random.Random) -> None:
    # Identical arms (no statistical winner) but no guardrail breach: a canary
    # whose job is *safety* still reaches 100% and promotes the new model.
    exp = two_arm_experiment(min_samples_per_arm=20)
    canary = CanaryRunner(
        exp, clock=FakeClock(step=1.0), ladder=(50.0, 100.0), min_samples_per_rung=20
    )
    _both(canary, 25, accept_p=0.7, fail_p=0.0, rng=rng)
    d1 = canary.evaluate()
    assert d1.traffic_percent == 100.0
    _both(canary, 25, accept_p=0.7, fail_p=0.0, rng=rng)
    d2 = canary.evaluate()
    assert d2.state is RolloutState.PROMOTED


def test_canary_rejects_bad_ladder() -> None:
    exp = two_arm_experiment()
    with pytest.raises(ValueError):
        CanaryRunner(exp, clock=FakeClock(), ladder=())
    with pytest.raises(ValueError):
        CanaryRunner(exp, clock=FakeClock(), ladder=(5.0, 1.0))  # not non-decreasing
    with pytest.raises(ValueError):
        CanaryRunner(exp, clock=FakeClock(), ladder=(0.0, 100.0))  # rung out of (0,100]


def test_canary_assignment_grows_with_rung(rng: random.Random) -> None:
    exp = two_arm_experiment(min_samples_per_arm=20)
    canary = CanaryRunner(exp, clock=FakeClock(step=1.0), min_samples_per_rung=20)
    units = [RenderUnit(book_id=f"b{i}") for i in range(4000)]

    enrolled_rung0 = sum(1 for u in units if canary.assign(u).in_experiment)
    _both(canary, 25, accept_p=0.8, fail_p=0.0, rng=rng)
    canary.evaluate()  # → 5%
    enrolled_rung1 = sum(1 for u in units if canary.assign(u).in_experiment)
    assert enrolled_rung1 > enrolled_rung0  # exposure grew


def test_canary_immediate_halt_on_first_rung(rng: random.Random) -> None:
    # A catastrophic new model is caught at 1% before any wide exposure.
    exp = two_arm_experiment(min_samples_per_arm=20, guardrail_margin=0.10)
    canary = CanaryRunner(exp, clock=FakeClock(step=1.0), min_samples_per_rung=20)
    _feed(canary, "control", 60, accept_p=0.8, fail_p=0.01, rng=rng)
    _feed(canary, "treat", 60, accept_p=0.8, fail_p=0.6, rng=rng)
    d = canary.evaluate()
    assert d.state is RolloutState.ROLLED_BACK
    assert canary.rung_index == 0  # never left the first rung
