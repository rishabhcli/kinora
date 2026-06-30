"""Paired analysis: quality/cost/latency/reliability blocks over a built dataset."""

from __future__ import annotations

import pytest

from app.video.shadow.analysis import analyze
from app.video.shadow.collector import ComparisonDataset, PairedSample
from app.video.shadow.seams import FailureKind, ShotSpec

from .test_video_shadow_fakes import make_outcome


def _build(
    deltas_and_costs: list[tuple[float, float, float]],
    *,
    base_quality: float = 0.6,
) -> ComparisonDataset:
    """Build a dataset from (quality_delta, cost_delta_s, latency_delta_ms) rows."""
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    for i, (dq, dcost, dlat) in enumerate(deltas_and_costs):
        prod = make_outcome("prod", quality=base_quality, video_seconds=5.0, latency_ms=1500.0)
        cand = make_outcome(
            "cand",
            quality=base_quality + dq,
            video_seconds=5.0 + dcost,
            latency_ms=1500.0 + dlat,
        )
        ds.add(
            PairedSample(
                shot_id=f"s{i}", production=prod, candidate=cand, spec=ShotSpec(shot_id=f"s{i}")
            )
        )
    return ds


async def test_quality_block_none_with_too_few_comparable() -> None:
    ds = _build([(0.1, 0.0, 0.0)])  # only one comparable pair
    ana = analyze(ds)
    assert ana.quality is None


async def test_quality_block_summarises_improvement() -> None:
    ds = _build([(0.2, 0.0, 0.0)] * 30)
    ana = analyze(ds)
    assert ana.quality is not None
    assert ana.quality.n_comparable == 30
    assert ana.quality.mean_delta == pytest.approx(0.2)
    assert ana.quality.win_rate == 1.0
    assert ana.quality.quality_ci_excludes_zero
    assert ana.quality.t_ci_low > 0.0


async def test_cost_and_latency_means() -> None:
    ds = _build([(0.1, -1.0, -300.0), (0.1, -3.0, -100.0)])
    ana = analyze(ds)
    assert ana.cost.mean_cost_delta_s == pytest.approx(-2.0)
    assert ana.cost.candidate_cheaper
    assert ana.latency.mean_latency_delta_ms == pytest.approx(-200.0)
    assert ana.latency.candidate_faster


async def test_total_video_seconds_aggregated() -> None:
    ds = _build([(0.1, 0.0, 0.0)] * 4)  # cand 5s each, prod 5s each
    ana = analyze(ds)
    assert ana.cost.candidate_total_video_seconds == pytest.approx(20.0)
    assert ana.cost.production_total_video_seconds == pytest.approx(20.0)


async def test_reliability_block_counts_candidate_failures() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    for i in range(8):
        prod = make_outcome("prod", quality=0.6)
        cand = make_outcome("cand", quality=0.7)
        ds.add(
            PairedSample(
                shot_id=f"ok{i}", production=prod, candidate=cand, spec=ShotSpec(shot_id=f"ok{i}")
            )
        )
    # 2 candidate provider failures.
    for i in range(2):
        prod = make_outcome("prod", quality=0.6)
        cand = make_outcome("cand", succeeded=False, failure=FailureKind.PROVIDER_ERROR)
        ds.add(
            PairedSample(
                shot_id=f"f{i}", production=prod, candidate=cand, spec=ShotSpec(shot_id=f"f{i}")
            )
        )
    ana = analyze(ds)
    assert ana.reliability.candidate.failure_rate == pytest.approx(0.2)
    assert ana.reliability.production.failure_rate == 0.0
    assert ana.reliability.failure_rate_delta == pytest.approx(0.2)


async def test_analysis_is_deterministic() -> None:
    ds = _build([(0.05 * ((i % 5) - 2), 0.0, 0.0) for i in range(40)])
    a = analyze(ds, bootstrap_seed=7)
    b = analyze(ds, bootstrap_seed=7)
    assert a.model_dump() == b.model_dump()
