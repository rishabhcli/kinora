"""Promotion recommendation thresholds: promote / hold / reject gate ordering."""

from __future__ import annotations

from app.video.shadow.analysis import analyze
from app.video.shadow.collector import ComparisonDataset, PairedSample
from app.video.shadow.recommendation import (
    PromotionThresholds,
    ReasonCode,
    Verdict,
    recommend,
)
from app.video.shadow.seams import FailureKind, ShotSpec

from .test_video_shadow_fakes import make_outcome


def _dataset(
    *,
    n: int,
    quality_delta: float,
    cost_delta: float = 0.0,
    candidate_failures: int = 0,
    base_quality: float = 0.6,
) -> ComparisonDataset:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    for i in range(n):
        prod = make_outcome("prod", quality=base_quality, video_seconds=5.0)
        cand = make_outcome(
            "cand", quality=base_quality + quality_delta, video_seconds=5.0 + cost_delta
        )
        ds.add(
            PairedSample(
                shot_id=f"s{i}", production=prod, candidate=cand, spec=ShotSpec(shot_id=f"s{i}")
            )
        )
    for j in range(candidate_failures):
        prod = make_outcome("prod", quality=base_quality)
        cand = make_outcome("cand", succeeded=False, failure=FailureKind.PROVIDER_ERROR)
        ds.add(
            PairedSample(
                shot_id=f"f{j}", production=prod, candidate=cand, spec=ShotSpec(shot_id=f"f{j}")
            )
        )
    return ds


def test_hold_when_too_few_samples() -> None:
    ds = _dataset(n=5, quality_delta=0.2)
    rec = recommend(analyze(ds), PromotionThresholds(min_comparable_samples=30))
    assert rec.verdict is Verdict.HOLD
    assert ReasonCode.INSUFFICIENT_SAMPLES in rec.reasons


def test_hold_when_no_comparable_pairs() -> None:
    # All candidate renders fail → no comparable pairs at all.
    ds = _dataset(n=0, quality_delta=0.0, candidate_failures=40)
    rec = recommend(analyze(ds))
    assert rec.verdict is Verdict.HOLD
    assert ReasonCode.NO_COMPARABLE_PAIRS in rec.reasons


def test_promote_on_clear_improvement() -> None:
    ds = _dataset(n=40, quality_delta=0.15)
    rec = recommend(analyze(ds))
    assert rec.verdict is Verdict.PROMOTE
    assert ReasonCode.ALL_GATES_PASSED in rec.reasons
    assert ReasonCode.QUALITY_IMPROVEMENT in rec.reasons
    assert rec.promote


def test_reject_on_quality_regression() -> None:
    ds = _dataset(n=40, quality_delta=-0.2)
    rec = recommend(analyze(ds))
    assert rec.verdict is Verdict.REJECT
    assert ReasonCode.QUALITY_REGRESSION in rec.reasons


def test_reject_on_cost_regression_even_with_better_quality() -> None:
    # Quality is better, but every shot costs 2 extra video-seconds.
    ds = _dataset(n=40, quality_delta=0.2, cost_delta=2.0)
    rec = recommend(analyze(ds), PromotionThresholds(max_cost_increase_s=0.0))
    assert rec.verdict is Verdict.REJECT
    assert ReasonCode.COST_REGRESSION in rec.reasons


def test_cost_regression_within_tolerance_is_allowed() -> None:
    ds = _dataset(n=40, quality_delta=0.2, cost_delta=0.5)
    rec = recommend(analyze(ds), PromotionThresholds(max_cost_increase_s=1.0))
    assert rec.verdict is Verdict.PROMOTE


def test_reject_on_reliability_regression() -> None:
    # 38 good pairs + 12 candidate failures → ~24% failure rate vs 2% tolerance.
    ds = _dataset(n=38, quality_delta=0.2, candidate_failures=12)
    rec = recommend(analyze(ds), PromotionThresholds(max_failure_rate_increase=0.02))
    assert rec.verdict is Verdict.REJECT
    assert ReasonCode.RELIABILITY_REGRESSION in rec.reasons


def test_hold_when_win_rate_below_floor_but_non_inferior() -> None:
    # Construct a dataset that is statistically non-inferior (CI low >= 0) but the
    # win-rate (after a dead-band) is under the floor: many tiny ties, a few wins.
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    # 35 exact ties (delta 0) and 5 wins of +0.1 → win-rate 5/40 = 0.125.
    for i in range(35):
        ds.add(
            PairedSample(
                shot_id=f"tie{i}",
                production=make_outcome("prod", quality=0.6),
                candidate=make_outcome("cand", quality=0.6),
                spec=ShotSpec(shot_id=f"tie{i}"),
            )
        )
    for i in range(5):
        ds.add(
            PairedSample(
                shot_id=f"win{i}",
                production=make_outcome("prod", quality=0.6),
                candidate=make_outcome("cand", quality=0.7),
                spec=ShotSpec(shot_id=f"win{i}"),
            )
        )
    ana = analyze(ds)
    assert ana.quality is not None
    # mean delta slightly positive, CI low ~ 0 (non-inferior), win-rate well below 0.5.
    rec = recommend(ana, PromotionThresholds(min_quality_ci_low=-0.05, min_win_rate=0.5))
    assert rec.verdict is Verdict.HOLD
    assert ReasonCode.WIN_RATE_BELOW_FLOOR in rec.reasons


def test_recommendation_carries_analysis_and_thresholds() -> None:
    ds = _dataset(n=40, quality_delta=0.15)
    th = PromotionThresholds()
    rec = recommend(analyze(ds), th)
    assert rec.candidate_model == "cand"
    assert rec.production_model == "prod"
    assert rec.thresholds == th
    assert rec.analysis.quality is not None
    # The summary explains the verdict.
    assert "canary" in rec.summary.lower()


def test_recommendation_is_deterministic() -> None:
    ds = _dataset(n=40, quality_delta=0.1)
    a = recommend(analyze(ds, bootstrap_seed=3))
    b = recommend(analyze(ds, bootstrap_seed=3))
    assert a.model_dump() == b.model_dump()
