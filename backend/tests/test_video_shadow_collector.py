"""Comparison collector: pairing, dedup, comparable subset, failure tallies."""

from __future__ import annotations

import pytest

from app.video.shadow.collector import (
    ComparisonDataset,
    PairedSample,
    candidate_failures,
    production_failures,
)
from app.video.shadow.seams import FailureKind, ShotSpec

from .test_video_shadow_fakes import make_outcome


def _sample(
    shot_id: str,
    *,
    prod_q: float | None,
    cand_q: float | None,
    prod_vs: float = 5.0,
    cand_vs: float = 5.0,
    prod_lat: float = 1500.0,
    cand_lat: float = 1000.0,
    prod_ok: bool = True,
    cand_ok: bool = True,
    cand_failure: FailureKind = FailureKind.NONE,
) -> PairedSample:
    return PairedSample(
        shot_id=shot_id,
        production=make_outcome(
            "prod", quality=prod_q, video_seconds=prod_vs, latency_ms=prod_lat, succeeded=prod_ok
        ),
        candidate=make_outcome(
            "cand",
            quality=cand_q,
            video_seconds=cand_vs,
            latency_ms=cand_lat,
            succeeded=cand_ok,
            failure=cand_failure,
        ),
        spec=ShotSpec(shot_id=shot_id),
    )


def test_quality_delta_only_when_both_scored() -> None:
    paired = _sample("s", prod_q=0.6, cand_q=0.8)
    assert paired.quality_delta == pytest.approx(0.2)
    # Missing candidate score → no delta.
    no_score = _sample("s", prod_q=0.6, cand_q=None)
    assert no_score.quality_delta is None
    # Candidate failed → no delta even if production scored.
    failed = _sample("s", prod_q=0.6, cand_q=0.8, cand_ok=False)
    assert failed.quality_delta is None


def test_cost_and_latency_deltas_signed() -> None:
    paired = _sample(
        "s", prod_q=0.6, cand_q=0.8, prod_vs=5.0, cand_vs=3.0, prod_lat=2000.0, cand_lat=800.0
    )
    assert paired.cost_delta == pytest.approx(-2.0)  # candidate cheaper
    assert paired.latency_delta_ms == pytest.approx(-1200.0)  # candidate faster


def test_dataset_dedup_by_shot_id_last_write_wins() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    ds.add(_sample("s1", prod_q=0.5, cand_q=0.6))
    ds.add(_sample("s1", prod_q=0.5, cand_q=0.9))  # replaces
    ds.add(_sample("s2", prod_q=0.5, cand_q=0.4))
    assert len(ds) == 2
    deltas = {s.shot_id: s.quality_delta for s in ds.comparable()}
    assert deltas["s1"] == pytest.approx(0.4)


def test_comparable_excludes_unscored_and_failed() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    ds.add(_sample("ok", prod_q=0.5, cand_q=0.7))
    ds.add(_sample("unscored", prod_q=0.5, cand_q=None))
    ds.add(_sample("failed", prod_q=0.5, cand_q=0.7, cand_ok=False))
    comparable = ds.comparable()
    assert [s.shot_id for s in comparable] == ["ok"]
    assert ds.quality_deltas() == pytest.approx([0.2])


def test_paired_qualities_aligned() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    ds.add(_sample("a", prod_q=0.5, cand_q=0.7))
    ds.add(_sample("b", prod_q=0.6, cand_q=0.6))
    prod, cand = ds.paired_qualities()
    assert prod == pytest.approx([0.5, 0.6])
    assert cand == pytest.approx([0.7, 0.6])


def test_cost_and_latency_deltas_over_both_succeeded_only() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    ds.add(_sample("a", prod_q=0.5, cand_q=0.7, prod_vs=5.0, cand_vs=4.0))
    ds.add(_sample("b", prod_q=0.5, cand_q=0.7, cand_ok=False))  # excluded
    assert ds.cost_deltas() == pytest.approx([-1.0])
    assert len(ds.latency_deltas_ms()) == 1


def test_failure_tallies_exclude_gated() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    ds.add(_sample("ok", prod_q=0.5, cand_q=0.7))
    ds.add(_sample("gated", prod_q=0.5, cand_q=0.7, cand_ok=False, cand_failure=FailureKind.GATED))
    ds.add(
        _sample(
            "err", prod_q=0.5, cand_q=0.7, cand_ok=False, cand_failure=FailureKind.PROVIDER_ERROR
        )
    )
    cand = candidate_failures(ds)
    # 3 attempts, 1 gated (excluded from denom), 1 success, 1 real failure.
    assert cand.attempts == 3
    assert cand.gated == 1
    assert cand.successes == 1
    assert cand.scored_attempts == 2
    assert cand.failure_rate == pytest.approx(0.5)
    assert cand.failures_by_kind == {"provider_error": 1}
    # Production all succeeded.
    prod = production_failures(ds)
    assert prod.failure_rate == 0.0


def test_dataset_round_trips_through_json() -> None:
    ds = ComparisonDataset(candidate_model="cand", production_model="prod")
    ds.add(_sample("a", prod_q=0.5, cand_q=0.7))
    dumped = ds.model_dump_json()
    restored = ComparisonDataset.model_validate_json(dumped)
    assert len(restored) == 1
    assert restored.quality_deltas() == pytest.approx([0.2])
