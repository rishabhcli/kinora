"""Temporal-coherence detection (§9.5 Motion) — flicker / morph / extra-limb.

All tests build grayscale grids directly (no PNG decode), so the pure analysis math
is exercised deterministically. Grids are small float-in-0..1 matrices.
"""

from __future__ import annotations

from app.render.qa.temporal import (
    flicker_score,
    limb_spike_score,
    morph_score,
    temporal_coherence,
)


def _const(value: float, size: int = 8) -> list[list[float]]:
    return [[value] * size for _ in range(size)]


def _checker(parity: int, size: int = 8) -> list[list[float]]:
    return [[1.0 if (r + c + parity) % 2 == 0 else 0.0 for c in range(size)] for r in range(size)]


# --------------------------------------------------------------------------- #
# Flicker — global luminance pulsing
# --------------------------------------------------------------------------- #


def test_flicker_clean_when_steady() -> None:
    frames = [_const(0.5), _const(0.5), _const(0.5)]
    assert flicker_score(frames) == 0.0


def test_flicker_high_on_luminance_pulse() -> None:
    # Whole frame jumps dark→bright→dark: the classic strobe.
    frames = [_const(0.1), _const(0.9), _const(0.1)]
    assert flicker_score(frames) > 0.8


def test_flicker_single_frame_is_zero() -> None:
    assert flicker_score([_const(0.5)]) == 0.0


# --------------------------------------------------------------------------- #
# Morph — structural churn beyond smooth motion
# --------------------------------------------------------------------------- #


def test_morph_clean_when_structure_stable() -> None:
    frames = [_checker(0), _checker(0), _checker(0)]
    assert morph_score(frames) == 0.0


def test_morph_high_on_structural_flip() -> None:
    # A frame whose entire checker pattern inverts churns far more than its neighbours.
    frames = [_checker(0), _checker(0), _checker(1), _checker(0)]
    assert morph_score(frames) > 0.5


# --------------------------------------------------------------------------- #
# Limb / topology spike — edge-energy outlier
# --------------------------------------------------------------------------- #


def test_limb_spike_clean_on_uniform_edges() -> None:
    frames = [_checker(0), _checker(0), _checker(0), _checker(0)]
    assert limb_spike_score(frames) == 0.0


def test_limb_spike_flags_edge_energy_outlier() -> None:
    # Three smooth (low-edge) frames + one busy (high-edge) frame = a topology spike.
    smooth = _const(0.5)
    busy = _checker(0)
    frames = [smooth, smooth, busy, smooth]
    assert limb_spike_score(frames) > 0.3


def test_limb_spike_too_few_frames() -> None:
    assert limb_spike_score([_checker(0), _checker(0)]) == 0.0


# --------------------------------------------------------------------------- #
# Combined report
# --------------------------------------------------------------------------- #


def test_temporal_coherence_clean_clip() -> None:
    frames = [_const(0.5), _const(0.5), _const(0.5), _const(0.5)]
    report = temporal_coherence(frames)
    assert report.motion_artifact == 0.0
    assert report.temporal == 1.0
    assert report.n_frames == 4


def test_temporal_coherence_broken_clip() -> None:
    # Strobing + structural churn → high artifact, low temporal goodness.
    frames = [_const(0.1), _checker(0), _const(0.9), _checker(1)]
    report = temporal_coherence(frames)
    assert report.motion_artifact > 0.4
    assert report.temporal < 0.6
    assert abs(report.temporal - (1.0 - report.motion_artifact)) < 1e-6


def test_temporal_coherence_empty() -> None:
    report = temporal_coherence([])
    assert report.motion_artifact == 0.0
    assert report.temporal == 1.0
    assert report.n_frames == 0
