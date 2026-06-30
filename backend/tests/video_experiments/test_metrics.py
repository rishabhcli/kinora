"""Metric collection: streaming proportion + mean aggregation from outcomes."""

from __future__ import annotations

import math

from app.video.experiments import (
    ACCEPT_RATE,
    COST_PER_SECOND,
    FAILURE_RATE,
    LATENCY_MS,
    QUALITY_SCORE,
    MetricCollector,
    RenderOutcome,
)


def test_failure_rate_proportion() -> None:
    c = MetricCollector()
    for _ in range(7):
        c.record(RenderOutcome("a", succeeded=True))
    for _ in range(3):
        c.record(RenderOutcome("a", succeeded=False))
    stat = c.proportion("a", FAILURE_RATE)
    assert stat.successes == 3 and stat.trials == 10
    assert math.isclose(stat.rate, 0.3)
    assert c.sample_count("a") == 10


def test_accept_rate_only_counts_known_verdicts() -> None:
    c = MetricCollector()
    c.record(RenderOutcome("a", succeeded=True, accepted=True))
    c.record(RenderOutcome("a", succeeded=True, accepted=True))
    c.record(RenderOutcome("a", succeeded=True, accepted=False))
    c.record(RenderOutcome("a", succeeded=False, accepted=None))  # failed: no accept verdict
    stat = c.proportion("a", ACCEPT_RATE)
    assert stat.successes == 2 and stat.trials == 3  # the failed one is excluded
    assert c.sample_count("a") == 4  # but counts as an attempt


def test_quality_and_latency_means() -> None:
    c = MetricCollector()
    for q in (0.8, 0.9, 1.0):
        c.record(RenderOutcome("a", quality_score=q, latency_ms=100.0))
    qs = c.mean("a", QUALITY_SCORE)
    assert qs.count == 3
    assert math.isclose(qs.mean, 0.9)
    lat = c.mean("a", LATENCY_MS)
    assert math.isclose(lat.mean, 100.0)
    assert math.isclose(lat.variance, 0.0)


def test_cost_normalized_per_second() -> None:
    c = MetricCollector()
    c.record(RenderOutcome("a", cost_usd=1.0, duration_s=5.0))  # 0.2/s
    c.record(RenderOutcome("a", cost_usd=0.6, duration_s=3.0))  # 0.2/s
    cps = c.mean("a", COST_PER_SECOND)
    assert math.isclose(cps.mean, 0.2)


def test_welford_variance_matches_textbook() -> None:
    c = MetricCollector()
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    for v in values:
        c.record(RenderOutcome("a", quality_score=v))
    stat = c.mean("a", QUALITY_SCORE)
    mean = sum(values) / len(values)
    expected_var = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    assert math.isclose(stat.mean, mean)
    assert math.isclose(stat.variance, expected_var, rel_tol=1e-9)


def test_extra_metrics_aggregate_as_means() -> None:
    c = MetricCollector()
    c.record(RenderOutcome("a", extra={"motion_smoothness": 0.7}))
    c.record(RenderOutcome("a", extra={"motion_smoothness": 0.9}))
    stat = c.mean("a", "motion_smoothness")
    assert stat.count == 2 and math.isclose(stat.mean, 0.8)


def test_unknown_arms_and_metrics_are_empty() -> None:
    c = MetricCollector()
    assert c.sample_count("ghost") == 0
    assert c.proportion("ghost", FAILURE_RATE).trials == 0
    assert c.mean("ghost", QUALITY_SCORE).count == 0
    c.record(RenderOutcome("a", succeeded=True))
    # an unknown proportion key on a real arm is empty, not an error
    assert c.proportion("a", "made_up").trials == 0
    assert set(c.arms()) == {"a"}
