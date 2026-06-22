"""Unit tests for the §13 metric math — pure, on synthetic / injected data.

Each metric is pinned to a *known* value computed against one-hot embeddings or
hand-arithmetic, so the definitions can't silently drift. These never touch the
network: CCS/style use injected embeddings; the async CCS wrapper uses a trivial
fake embedder to confirm it wires embed → pure correctly.
"""

from __future__ import annotations

import math

from app.eval.metrics import (
    BufferSample,
    accepted_footage_efficiency,
    buffer_health,
    ccs_from_embeddings,
    character_consistency_score,
    latency_to_first_frame,
    regeneration_rate,
    style_drift,
)

_DIM = 8


def one_hot(axis: int, *, dim: int = _DIM) -> list[float]:
    """A unit one-hot vector — cosine(one_hot(i), one_hot(j)) = 1 if i==j else 0."""
    vec = [0.0] * dim
    vec[axis % dim] = 1.0
    return vec


# --------------------------------------------------------------------------- #
# CCS (§13)
# --------------------------------------------------------------------------- #


def test_ccs_identical_crops_is_one() -> None:
    crops = [one_hot(0), one_hot(0), one_hot(0)]
    assert ccs_from_embeddings(crops, one_hot(0)) == 1.0


def test_ccs_orthogonal_crops_is_zero() -> None:
    crops = [one_hot(1), one_hot(2), one_hot(3)]
    assert ccs_from_embeddings(crops, one_hot(0)) == 0.0


def test_ccs_half_matching_is_one_half() -> None:
    # Two crops match the ref (cos 1), two are orthogonal (cos 0) -> mean 0.5.
    crops = [one_hot(0), one_hot(0), one_hot(1), one_hot(2)]
    assert ccs_from_embeddings(crops, one_hot(0)) == 0.5


def test_ccs_empty_is_zero() -> None:
    assert ccs_from_embeddings([], one_hot(0)) == 0.0


async def test_character_consistency_score_wraps_embedder() -> None:
    class FixedEmbedder:
        async def embed_images(self, images: list[bytes]) -> list[list[float]]:
            # Every crop -> axis 0; the locked ref -> axis 0 too (perfect match).
            return [one_hot(0) for _ in images]

        async def embed_texts(self, texts: list[str]) -> list[list[float]]:
            return [one_hot(0) for _ in texts]

    score = await character_consistency_score(
        [b"a", b"b"], b"ref", embedder=FixedEmbedder()
    )
    assert score == 1.0


# --------------------------------------------------------------------------- #
# Accepted-footage efficiency (§13)
# --------------------------------------------------------------------------- #


def test_efficiency_basic() -> None:
    assert accepted_footage_efficiency(100.0, 20.0) == 80.0


def test_efficiency_nothing_generated_is_100() -> None:
    assert accepted_footage_efficiency(0.0, 0.0) == 100.0


def test_efficiency_clamped_to_zero() -> None:
    assert accepted_footage_efficiency(10.0, 25.0) == 0.0


# --------------------------------------------------------------------------- #
# Regeneration rate (§13)
# --------------------------------------------------------------------------- #


def test_regeneration_rate() -> None:
    assert regeneration_rate(2, 10) == 0.2


def test_regeneration_rate_no_shots() -> None:
    assert regeneration_rate(0, 0) == 0.0


# --------------------------------------------------------------------------- #
# Style drift (§13)
# --------------------------------------------------------------------------- #


def test_style_drift_identical_is_zero() -> None:
    assert style_drift([one_hot(0), one_hot(0), one_hot(0)]) == 0.0


def test_style_drift_two_orthogonal_is_one_half() -> None:
    # centroid = [0.5, 0.5, 0...]; each ||e - c||^2 = 0.5; mean = 0.5.
    assert style_drift([one_hot(0), one_hot(1)]) == 0.5


def test_style_drift_single_is_zero() -> None:
    assert style_drift([one_hot(0)]) == 0.0


# --------------------------------------------------------------------------- #
# Latency-to-first-frame (§13, §4.8)
# --------------------------------------------------------------------------- #


def test_latency_to_first_frame() -> None:
    lat = latency_to_first_frame(seek_ts=10.0, first_coherent_ts=10.05, first_full_video_ts=22.0)
    assert math.isclose(lat.coherent_s, 0.05, abs_tol=1e-9)
    assert lat.full_video_s == 12.0


def test_latency_clamps_negative() -> None:
    lat = latency_to_first_frame(seek_ts=10.0, first_coherent_ts=9.0, first_full_video_ts=9.5)
    assert lat.coherent_s == 0.0
    assert lat.full_video_s == 0.0


# --------------------------------------------------------------------------- #
# Buffer health (§13, §4.10)
# --------------------------------------------------------------------------- #


def _trace(
    values: list[float], *, low: float = 25.0, high: float = 75.0, dt: float = 2.5
) -> list[BufferSample]:
    return [
        BufferSample(t=i * dt, committed_seconds_ahead=v, low=low, high=high)
        for i, v in enumerate(values)
    ]


def test_buffer_health_always_above_low_no_stalls() -> None:
    health = buffer_health(_trace([75, 70, 40, 30, 75, 60, 40, 30]))
    assert health.fraction_above_low == 1.0
    assert health.stalls == 0
    assert health.samples == 8


def test_buffer_health_counts_stalls_and_below_low_time() -> None:
    # Dips to 0 once (a visible stall) then recovers; one interval below L.
    health = buffer_health(_trace([75, 50, 0, 60, 75]))
    assert health.stalls == 1
    assert 0.0 < health.fraction_above_low < 1.0


def test_buffer_health_empty_trace() -> None:
    health = buffer_health([])
    assert health.fraction_above_low == 1.0
    assert health.stalls == 0
    assert health.samples == 0


def test_buffer_sample_to_contract_shape() -> None:
    sample = BufferSample(t=2.5, committed_seconds_ahead=41.0, low=25.0, high=75.0)
    assert sample.to_contract() == {
        "t": 2.5,
        "committed_seconds_ahead": 41.0,
        "low": 25.0,
        "high": 75.0,
    }
