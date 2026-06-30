"""Statistics engine: significance, CI correctness, direction-aware guardrails."""

from __future__ import annotations

import math

from app.flags.stats import ProportionStat, SampleStat
from app.video.experiments import (
    MetricDirection,
    MetricKind,
    VideoMetric,
    compare_mean,
    compare_proportion,
    guardrail_breach,
)


def _prop_metric(direction: MetricDirection, **kw: object) -> VideoMetric:
    return VideoMetric("m", MetricKind.PROPORTION, direction, **kw)  # type: ignore[arg-type]


def _mean_metric(direction: MetricDirection, **kw: object) -> VideoMetric:
    return VideoMetric("m", MetricKind.MEAN, direction, **kw)  # type: ignore[arg-type]


# --- significance ----------------------------------------------------------- #


def test_proportion_significant_improvement() -> None:
    metric = _prop_metric(MetricDirection.INCREASE)
    control = ProportionStat(600, 1000)  # 60%
    treatment = ProportionStat(850, 1000)  # 85%
    cmp = compare_proportion(metric, control, treatment)
    assert cmp.significant
    assert cmp.treatment_better
    assert cmp.significant_win
    assert not cmp.significant_regression
    assert cmp.absolute_diff > 0
    assert math.isclose(cmp.relative_change, (0.85 - 0.6) / 0.6, rel_tol=1e-6)


def test_proportion_no_difference_is_not_significant() -> None:
    metric = _prop_metric(MetricDirection.INCREASE)
    cmp = compare_proportion(metric, ProportionStat(500, 1000), ProportionStat(505, 1000))
    assert not cmp.significant
    assert not cmp.significant_win


def test_direction_decrease_treats_lower_as_better() -> None:
    # failure rate: lower is better. treatment 5% vs control 20%.
    metric = _prop_metric(MetricDirection.DECREASE)
    cmp = compare_proportion(metric, ProportionStat(200, 1000), ProportionStat(50, 1000))
    assert cmp.significant
    assert cmp.treatment_better  # went down → good
    assert cmp.absolute_diff < 0  # raw diff is negative but that's *good* here


def test_ci_brackets_the_true_difference() -> None:
    # With large, clean samples the always-valid CI must contain the true diff.
    metric = _prop_metric(MetricDirection.INCREASE)
    control = ProportionStat(700, 5000)  # 0.14
    treatment = ProportionStat(900, 5000)  # 0.18
    cmp = compare_proportion(metric, control, treatment)
    true_diff = 0.18 - 0.14
    assert cmp.ci_low <= true_diff <= cmp.ci_high
    assert cmp.ci_low <= cmp.absolute_diff <= cmp.ci_high


def test_fixed_horizon_proportion_path() -> None:
    metric = _prop_metric(MetricDirection.INCREASE)
    cmp = compare_proportion(
        metric, ProportionStat(600, 1000), ProportionStat(850, 1000), sequential=False
    )
    assert cmp.significant and cmp.treatment_better
    # fixed-horizon CI is narrower than the sequential one (price of peeking).
    seq = compare_proportion(metric, ProportionStat(600, 1000), ProportionStat(850, 1000))
    assert (cmp.ci_high - cmp.ci_low) < (seq.ci_high - seq.ci_low)


# --- Welch mean ------------------------------------------------------------- #


def test_mean_cheaper_treatment_is_winner() -> None:
    metric = _mean_metric(MetricDirection.DECREASE)  # cost per second
    control = SampleStat(200, 0.50, 0.0025)
    treatment = SampleStat(200, 0.30, 0.0025)
    cmp = compare_mean(metric, control, treatment)
    assert cmp.significant and cmp.treatment_better
    assert cmp.absolute_diff < 0
    assert cmp.ci_low <= -0.20 <= cmp.ci_high


def test_mean_no_signal_below_two_samples() -> None:
    metric = _mean_metric(MetricDirection.INCREASE)
    cmp = compare_mean(metric, SampleStat(1, 0.5, 0.0), SampleStat(1, 0.9, 0.0))
    assert not cmp.significant


# --- guardrail breach ------------------------------------------------------- #


def test_guardrail_breach_on_significant_regression() -> None:
    # accept rate is INCREASE; treatment dropped a lot → breach.
    metric = _prop_metric(MetricDirection.INCREASE, is_guardrail=True, guardrail_margin=0.05)
    cmp = compare_proportion(metric, ProportionStat(800, 1000), ProportionStat(500, 1000))
    verdict = guardrail_breach(metric, cmp, min_samples=100)
    assert verdict.breached


def test_guardrail_not_breached_within_margin() -> None:
    # a tiny, tolerated dip (within the 20% relative margin) is not a breach.
    metric = _prop_metric(MetricDirection.INCREASE, is_guardrail=True, guardrail_margin=0.20)
    cmp = compare_proportion(metric, ProportionStat(800, 5000), ProportionStat(792, 5000))
    verdict = guardrail_breach(metric, cmp, min_samples=100)
    assert not verdict.breached


def test_guardrail_not_breached_on_improvement() -> None:
    metric = _prop_metric(MetricDirection.INCREASE, is_guardrail=True, guardrail_margin=0.05)
    cmp = compare_proportion(metric, ProportionStat(500, 1000), ProportionStat(800, 1000))
    verdict = guardrail_breach(metric, cmp, min_samples=100)
    assert not verdict.breached  # going UP on an INCREASE metric is good


def test_guardrail_decrease_metric_breaches_when_rises() -> None:
    # failure rate is DECREASE; treatment failure rate rose a lot → breach.
    metric = _prop_metric(MetricDirection.DECREASE, is_guardrail=True, guardrail_margin=0.10)
    cmp = compare_proportion(metric, ProportionStat(20, 1000), ProportionStat(200, 1000))
    verdict = guardrail_breach(metric, cmp, min_samples=100)
    assert verdict.breached


def test_guardrail_waits_for_minimum_samples() -> None:
    metric = _prop_metric(MetricDirection.INCREASE, is_guardrail=True, guardrail_margin=0.05)
    cmp = compare_proportion(metric, ProportionStat(8, 10), ProportionStat(2, 10))
    verdict = guardrail_breach(metric, cmp, min_samples=100)
    assert not verdict.breached  # not enough data → never rollback on noise
    assert "insufficient samples" in verdict.detail


def test_guardrail_breaches_against_zero_baseline() -> None:
    # control failure rate is exactly 0; treatment fails a lot. The relative
    # margin is undefined against a zero baseline, so a significant regression
    # must still be a breach (not silently swallowed as "within margin").
    metric = _prop_metric(MetricDirection.DECREASE, is_guardrail=True, guardrail_margin=0.10)
    cmp = compare_proportion(metric, ProportionStat(0, 1000), ProportionStat(400, 1000))
    verdict = guardrail_breach(metric, cmp, min_samples=100)
    assert verdict.breached
    assert "zero baseline" in verdict.detail


def test_non_guardrail_metric_never_breaches() -> None:
    metric = _prop_metric(MetricDirection.INCREASE, is_guardrail=False)
    cmp = compare_proportion(metric, ProportionStat(800, 1000), ProportionStat(100, 1000))
    verdict = guardrail_breach(metric, cmp, min_samples=10)
    assert not verdict.breached
