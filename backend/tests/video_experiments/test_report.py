"""Report decisioning: promote / hold / rollback at the current data."""

from __future__ import annotations

import random
from typing import cast

from app.video.experiments import (
    ACCEPT_RATE,
    COST_PER_SECOND,
    MetricCollector,
    MetricDirection,
    MetricKind,
    Recommendation,
    RenderOutcome,
    VideoExperiment,
    VideoMetric,
    VideoVariant,
    build_report,
)

from .conftest import feed as _fill
from .conftest import two_arm_experiment


def test_promote_clear_winner() -> None:
    exp = two_arm_experiment(min_samples_per_arm=50)
    c = MetricCollector()
    rng = random.Random(1)
    _fill(c, "control", 300, accept_p=0.60, fail_p=0.01, rng=rng)
    _fill(c, "treat", 300, accept_p=0.85, fail_p=0.01, rng=rng)
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.PROMOTE
    assert report.winner_key == "treat"
    assert report.rollback_keys == ()
    arm = next(a for a in report.arms if a.variant_key == "treat")
    assert arm.is_winner and not arm.breached


def test_rollback_when_guardrail_breached() -> None:
    exp = two_arm_experiment(min_samples_per_arm=50, guardrail_margin=0.10)
    c = MetricCollector()
    rng = random.Random(2)
    _fill(c, "control", 300, accept_p=0.7, fail_p=0.02, rng=rng)
    _fill(c, "treat", 300, accept_p=0.7, fail_p=0.45, rng=rng)  # failure rate explodes
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.ROLLBACK
    assert "treat" in report.rollback_keys
    assert report.winner_key is None


def test_rollback_dominates_even_if_primary_wins() -> None:
    # treatment has a better accept rate AND a blown failure rate → rollback.
    exp = two_arm_experiment(min_samples_per_arm=50, guardrail_margin=0.10)
    c = MetricCollector()
    rng = random.Random(3)
    _fill(c, "control", 300, accept_p=0.5, fail_p=0.02, rng=rng)
    _fill(c, "treat", 300, accept_p=0.9, fail_p=0.5, rng=rng)
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.ROLLBACK


def test_hold_below_sample_floor() -> None:
    exp = two_arm_experiment(min_samples_per_arm=200)
    c = MetricCollector()
    rng = random.Random(4)
    _fill(c, "control", 30, accept_p=0.6, fail_p=0.0, rng=rng)
    _fill(c, "treat", 30, accept_p=0.9, fail_p=0.0, rng=rng)
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.HOLD
    assert "floor" in report.rationale or "winner" in report.rationale


def test_hold_when_inconclusive() -> None:
    exp = two_arm_experiment(min_samples_per_arm=50)
    c = MetricCollector()
    rng = random.Random(5)
    _fill(c, "control", 200, accept_p=0.7, fail_p=0.0, rng=rng)
    _fill(c, "treat", 200, accept_p=0.7, fail_p=0.0, rng=rng)  # identical
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.HOLD


def test_report_picks_strongest_winner_among_many() -> None:
    exp = VideoExperiment(
        "multi",
        (
            VideoVariant("control", "p", "m0", 4000, is_control=True),
            VideoVariant("t_small", "p", "m1", 3000),
            VideoVariant("t_big", "p", "m2", 3000),
        ),
        salt="s",
        metrics=(VideoMetric(ACCEPT_RATE, MetricKind.PROPORTION, MetricDirection.INCREASE),),
        min_samples_per_arm=50,
    )
    c = MetricCollector()
    rng = random.Random(6)
    _fill(c, "control", 400, accept_p=0.50, fail_p=0.0, rng=rng)
    _fill(c, "t_small", 400, accept_p=0.62, fail_p=0.0, rng=rng)
    _fill(c, "t_big", 400, accept_p=0.85, fail_p=0.0, rng=rng)
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.PROMOTE
    assert report.winner_key == "t_big"  # the bigger lift wins


def test_report_serialization_and_text() -> None:
    exp = two_arm_experiment(min_samples_per_arm=50)
    c = MetricCollector()
    rng = random.Random(7)
    _fill(c, "control", 200, accept_p=0.6, fail_p=0.0, rng=rng)
    _fill(c, "treat", 200, accept_p=0.85, fail_p=0.0, rng=rng)
    report = build_report(exp, c)
    d = report.to_dict()
    assert d["recommendation"] == "promote"
    assert d["primary_metric"] == ACCEPT_RATE
    arms = cast("list[dict[str, object]]", d["arms"])
    assert any(a["variant_key"] == "treat" for a in arms)
    text = report.render_text()
    assert "PROMOTE" in text and "treat" in text


def test_cost_per_second_winner_via_mean_metric() -> None:
    exp = VideoExperiment(
        "cost",
        (
            VideoVariant("control", "p", "old", 5000, is_control=True),
            VideoVariant("treat", "p", "new", 5000),
        ),
        salt="s",
        metrics=(VideoMetric(COST_PER_SECOND, MetricKind.MEAN, MetricDirection.DECREASE),),
        min_samples_per_arm=30,
    )
    c = MetricCollector()
    rng = random.Random(8)
    for _ in range(120):
        c.record(RenderOutcome("control", cost_usd=0.5 + rng.gauss(0, 0.02), duration_s=1.0))
        c.record(RenderOutcome("treat", cost_usd=0.3 + rng.gauss(0, 0.02), duration_s=1.0))
    report = build_report(exp, c)
    assert report.recommendation is Recommendation.PROMOTE
    assert report.winner_key == "treat"
