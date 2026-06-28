"""Significance-math tests — fixed-horizon and sequential-safe."""

from __future__ import annotations

import random

import pytest

from app.flags.errors import StatsError
from app.flags.stats import (
    ProportionStat,
    SampleStat,
    guardrail_breached,
    msprt_proportion,
    relative_uplift,
    required_sample_size,
    two_proportion_ztest,
    two_sided_p,
    welch_ttest,
)


def test_proportion_validation() -> None:
    with pytest.raises(StatsError):
        ProportionStat(10, 5)  # successes > trials
    with pytest.raises(StatsError):
        ProportionStat(-1, 5)
    assert ProportionStat(0, 0).rate == 0.0


def test_ztest_detects_clear_difference() -> None:
    r = two_proportion_ztest(ProportionStat(100, 1000), ProportionStat(160, 1000))
    assert r.significant
    assert r.p_value < 0.01
    assert r.estimate == pytest.approx(0.06, abs=1e-9)
    assert r.ci_low < r.estimate < r.ci_high


def test_ztest_no_difference_not_significant() -> None:
    r = two_proportion_ztest(ProportionStat(500, 5000), ProportionStat(505, 5000))
    assert not r.significant
    assert r.p_value > 0.5


def test_ztest_empty_arm_is_safe() -> None:
    r = two_proportion_ztest(ProportionStat(0, 0), ProportionStat(5, 10))
    assert not r.significant
    assert r.p_value == 1.0


def test_ztest_alpha_validated() -> None:
    with pytest.raises(StatsError):
        two_proportion_ztest(ProportionStat(1, 2), ProportionStat(1, 2), alpha=0)
    with pytest.raises(StatsError):
        two_proportion_ztest(ProportionStat(1, 2), ProportionStat(1, 2), alpha=1)


def test_welch_detects_mean_shift() -> None:
    a = SampleStat.from_values([float(x % 5) for x in range(500)])
    b = SampleStat.from_values([float(x % 5) + 1.0 for x in range(500)])
    r = welch_ttest(a, b)
    assert r.significant
    assert r.estimate == pytest.approx(1.0, abs=1e-9)


def test_welch_small_sample_is_safe() -> None:
    r = welch_ttest(SampleStat(1, 1.0, 0.0), SampleStat(1, 2.0, 0.0))
    assert not r.significant


def test_sample_stat_from_values() -> None:
    s = SampleStat.from_values([2.0, 4.0, 6.0])
    assert s.count == 3
    assert s.mean == pytest.approx(4.0)
    assert s.variance == pytest.approx(4.0)  # sample variance


def test_msprt_decisive_on_real_effect() -> None:
    mv = msprt_proportion(ProportionStat(100, 1000), ProportionStat(170, 1000))
    assert mv.decisive
    assert mv.p_value < 0.05
    assert mv.ci_low < mv.estimate < mv.ci_high


def test_msprt_not_decisive_on_null() -> None:
    mv = msprt_proportion(ProportionStat(500, 5000), ProportionStat(498, 5000))
    assert not mv.decisive
    assert mv.p_value > 0.05


def test_msprt_is_more_conservative_than_fixed_horizon() -> None:
    # Always-valid p-value should never be smaller than the fixed-horizon p-value
    # at the same data (that conservatism is what buys peeking-safety).
    c, t = ProportionStat(300, 4000), ProportionStat(340, 4000)
    assert msprt_proportion(c, t).p_value >= two_proportion_ztest(c, t).p_value


def test_msprt_controls_type_one_error_under_peeking() -> None:
    # Simulate many A/A experiments with continuous peeking; the fraction that
    # EVER cross the always-valid threshold must stay near/under alpha (0.05).
    rng = random.Random(20260628)
    alpha = 0.05
    rate = 0.2
    false_positives = 0
    trials = 300
    for _ in range(trials):
        c_succ = t_succ = 0
        crossed = False
        for step in range(400):  # peek after every pair of observations
            n = step + 1
            if rng.random() < rate:
                c_succ += 1
            if rng.random() < rate:
                t_succ += 1
            if n >= 30:  # let a little data accumulate
                mv = msprt_proportion(
                    ProportionStat(c_succ, n), ProportionStat(t_succ, n), alpha=alpha
                )
                if mv.decisive:
                    crossed = True
                    break
        if crossed:
            false_positives += 1
    # Always-valid guarantee: P(ever reject under H0) <= alpha. Allow generous
    # Monte-Carlo slack but it must be far below the inflated peeking rate (~0.3+).
    assert false_positives / trials < 0.12


def test_guardrail_breach_detection() -> None:
    # treatment clearly worse than control on a "higher is better" metric
    assert guardrail_breached(
        ProportionStat(800, 1000), ProportionStat(600, 1000), max_relative_regression=0.02
    )
    # treatment within tolerance -> no breach
    assert not guardrail_breached(
        ProportionStat(800, 1000), ProportionStat(800, 1000), max_relative_regression=0.02
    )


def test_relative_uplift() -> None:
    assert relative_uplift(0.2, 0.24) == pytest.approx(0.2)
    assert relative_uplift(0.0, 0.1) == 0.0


def test_required_sample_size_reasonable() -> None:
    n = required_sample_size(0.1, mde=0.1)
    # ballpark for detecting a 10% relative lift on a 10% baseline
    assert 5_000 < n < 50_000
    # bigger MDE => smaller sample
    assert required_sample_size(0.1, mde=0.2) < n


def test_required_sample_size_validates() -> None:
    with pytest.raises(StatsError):
        required_sample_size(0.0, mde=0.1)
    with pytest.raises(StatsError):
        required_sample_size(0.1, mde=0.0)


def test_two_sided_p_symmetry() -> None:
    assert two_sided_p(1.96) == pytest.approx(0.05, abs=0.005)
    assert two_sided_p(-1.96) == pytest.approx(0.05, abs=0.005)
    assert two_sided_p(0.0) == pytest.approx(1.0)
