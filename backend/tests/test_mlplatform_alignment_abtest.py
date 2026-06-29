"""Tests for the offline A/B + win-rate harness and tournament ranking."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pytest

from app.mlplatform.alignment.abtest import (
    WinRateHarness,
    reward_arm,
    tournament,
)
from app.mlplatform.alignment.errors import DataError
from app.mlplatform.alignment.reward_model import RewardModel, RewardModelTrainer
from app.mlplatform.alignment.types import Sample, SampleDataset


def _gold() -> RewardModel:
    # Gold prefers higher first feature (clean linear truth).
    rng = np.random.default_rng(0)
    samples = []
    for _ in range(400):
        x = float(rng.uniform(0, 1))
        y = float(rng.uniform(0, 1))
        samples.append(Sample([x, y], 1.0 if x >= 0.5 else 0.0))
    return RewardModelTrainer(l2=0.01).fit(SampleDataset(samples=tuple(samples)))


def _eval_set(n: int = 100, seed: int = 1) -> list[tuple[list[float], list[float]]]:
    rng = np.random.default_rng(seed)
    return [
        (list(rng.uniform(0, 1, size=2)), list(rng.uniform(0, 1, size=2)))
        for _ in range(n)
    ]


# Arms used across the A/B tests (defs, not lambdas, per lint policy).
def _arm_first(f: Sequence[float]) -> float:  # picks by the correct first feature
    return f[0]


def _arm_second(f: Sequence[float]) -> float:  # picks by the irrelevant second feature
    return f[1]


def _arm_inverted(f: Sequence[float]) -> float:  # actively anti-correlated with the truth
    return -f[0]


def test_good_arm_beats_bad_arm() -> None:
    gold = _gold()
    harness = WinRateHarness(gold=gold, n_bootstrap=500, seed=42)
    # Arm A picks by the (correct) first feature; Arm B picks by the irrelevant
    # second feature.
    res = harness.compare(_arm_first, _arm_second, _eval_set(), name_a="good", name_b="bad")
    assert res.win_rate > 0.5
    assert res.winner == "good"
    assert res.ci_low <= res.win_rate <= res.ci_high


def test_identical_arms_tie() -> None:
    gold = _gold()
    harness = WinRateHarness(gold=gold, n_bootstrap=300, seed=7)
    res = harness.compare(_arm_first, _arm_first, _eval_set())
    # Same arm picks the same option => every context is a tie.
    assert res.win_rate == pytest.approx(0.5)
    assert res.ties == res.n
    assert res.winner == "tie"


def test_significance_flagged_for_clear_winner() -> None:
    gold = _gold()
    harness = WinRateHarness(gold=gold, n_bootstrap=1000, seed=3)
    res = harness.compare(_arm_first, _arm_inverted, _eval_set(n=200))
    assert res.win_rate > 0.7
    assert res.significant
    assert res.p_value < 0.05


def test_empty_eval_set_raises() -> None:
    harness = WinRateHarness(gold=_gold())
    with pytest.raises(DataError):
        harness.compare(_arm_first, _arm_second, [])


def test_bootstrap_ci_is_deterministic() -> None:
    gold = _gold()
    h1 = WinRateHarness(gold=gold, n_bootstrap=400, seed=11)
    h2 = WinRateHarness(gold=gold, n_bootstrap=400, seed=11)
    ev = _eval_set(seed=2)
    r1 = h1.compare(_arm_first, _arm_second, ev)
    r2 = h2.compare(_arm_first, _arm_second, ev)
    assert r1.ci_low == pytest.approx(r2.ci_low)
    assert r1.ci_high == pytest.approx(r2.ci_high)


def test_tournament_ranks_best_first() -> None:
    gold = _gold()
    harness = WinRateHarness(gold=gold, n_bootstrap=200, seed=5)
    arms = {
        "perfect": _arm_first,  # uses the right feature
        "noise": _arm_second,  # ignores the right feature
        "inverted": _arm_inverted,  # actively wrong
    }
    res = tournament(harness, arms, _eval_set(n=120))
    assert res.ranking[0] == "perfect"
    assert res.ranking[-1] == "inverted"
    # Diagonal ties.
    assert np.allclose(np.diag(res.win_matrix), 0.5)
    # win_matrix is roughly antisymmetric about 0.5 (no ties expected here).
    for i in range(3):
        for j in range(3):
            if i != j:
                assert res.win_matrix[i, j] + res.win_matrix[j, i] == pytest.approx(
                    1.0, abs=1e-9
                )


def test_tournament_needs_two_arms() -> None:
    harness = WinRateHarness(gold=_gold())
    with pytest.raises(DataError):
        tournament(harness, {"only": _arm_first}, _eval_set())


def test_reward_arm_adapter() -> None:
    gold = _gold()
    arm = reward_arm(gold)
    # The adapter scores by reward; a high-x candidate scores above a low-x one.
    assert arm([0.9, 0.5]) > arm([0.1, 0.5])
