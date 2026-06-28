"""Robust temporal-coherence detection — flicker / morph / extra-limb (§9.5 Motion).

The §9.5 Critic's motion check is a single VL-rated 0..1 artifact score. A VL pass is
expensive and subjective; this module adds **cheap, deterministic, perceptually-
grounded** temporal signals computed directly from the decoded frame sequence, so a
clip with obvious mechanical defects is caught without (or alongside) the VL call:

* **flicker** — large frame-to-frame *global luminance* swings (the whole frame
  pulsing brighter/darker), the classic diffusion-video strobe;
* **morph** — large frame-to-frame *structural* change (the layout of light/dark
  regions churning) beyond what smooth motion produces, i.e. the subject melting /
  re-forming between frames;
* **limb / topology spike** — a sudden jump in *edge density* (a spurious extra arm,
  finger, or duplicated feature briefly appears), detected as an outlier in the
  per-frame edge-energy series.

Each signal is a 0..1 *artifact* score (0 = clean). They combine into one
``motion_artifact``-compatible number plus a 0..1 ``temporal`` *goodness* score for
the reward features. The math is pure over a list of grayscale frames (2-D float
grids in 0..1); :func:`frames_to_gray` decodes PNG/JPEG ``bytes`` via Pillow, but the
analysis functions never touch Pillow, so they unit-test on hand-built grids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

#: A grayscale frame: rows of pixel luminances in 0..1.
Gray = list[list[float]]

#: Weights blending the three artifact signals into one 0..1 motion-artifact score.
_W_FLICKER, _W_MORPH, _W_LIMB = 0.4, 0.4, 0.2
#: A frame-to-frame mean-luminance jump (in 0..1) at/above this reads as full flicker.
_FLICKER_FULL = 0.35
#: A frame-to-frame structural-difference (mean |Δ|) at/above this reads as full morph.
_MORPH_FULL = 0.30
#: Edge-energy outlier (robust-z) at/above this reads as a full limb/topology spike.
_LIMB_FULL_Z = 6.0
#: Minimum edge-energy scale for the limb-spike z-score, so a clip whose calm frames
#: all share (near-)zero edge energy — collapsing MAD to 0 — still detects a busy
#: outlier frame instead of dividing by zero. A small fraction of the 0..1 range.
_LIMB_SCALE_FLOOR = 0.02


@dataclass(frozen=True, slots=True)
class TemporalReport:
    """The temporal-coherence verdict for one clip."""

    flicker: float = 0.0
    morph: float = 0.0
    limb_spike: float = 0.0
    #: Combined 0..1 artifact score (0 = clean) — comparable to ``motion_artifact``.
    motion_artifact: float = 0.0
    #: 0..1 *goodness* score for the reward features (``1 - motion_artifact``).
    temporal: float = 1.0
    n_frames: int = 0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _median(values: Sequence[float]) -> float:
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return s[mid] if n % 2 else 0.5 * (s[mid - 1] + s[mid])


def _frame_mean(frame: Gray) -> float:
    flat = [v for row in frame for v in row]
    return _mean(flat)


def _structural_diff(a: Gray, b: Gray) -> float:
    """Mean absolute per-pixel difference between two same-shape frames (0..1)."""
    total = 0.0
    count = 0
    for ra, rb in zip(a, b, strict=False):
        for va, vb in zip(ra, rb, strict=False):
            total += abs(va - vb)
            count += 1
    return total / count if count else 0.0


def _edge_energy(frame: Gray) -> float:
    """Mean local gradient magnitude — a proxy for edge density / structure count.

    A sudden jump in edge energy across the sequence betrays a spurious feature
    (an extra limb / finger) briefly appearing. Computed as the mean of horizontal
    + vertical first differences, so it needs no convolution kernel.
    """
    total = 0.0
    count = 0
    rows = len(frame)
    for y in range(rows):
        cols = len(frame[y])
        for x in range(cols):
            if x + 1 < cols:
                total += abs(frame[y][x + 1] - frame[y][x])
                count += 1
            if y + 1 < rows:
                total += abs(frame[y + 1][x] - frame[y][x])
                count += 1
    return total / count if count else 0.0


def flicker_score(frames: Sequence[Gray]) -> float:
    """Max frame-to-frame global-luminance jump, normalized to a 0..1 artifact."""
    if len(frames) < 2:
        return 0.0
    means = [_frame_mean(f) for f in frames]
    jumps = [abs(means[i] - means[i - 1]) for i in range(1, len(means))]
    return _clamp01(max(jumps) / _FLICKER_FULL)


def morph_score(frames: Sequence[Gray]) -> float:
    """Max frame-to-frame structural difference above a smooth-motion baseline.

    Uses the *minimum* consecutive structural diff as the smooth-motion baseline (the
    quietest transition the clip achieves) and flags the worst frame's *excess* over
    it. Steady panning produces uniformly-moderate diffs ⇒ small excess ⇒ no morph
    flag; a frame whose content churns far more than the clip's calmest transition
    (the subject melting / re-forming) produces a large excess. Using the min as the
    baseline (not the median) means even *repeated* churn — a morph in and back out —
    is still caught, since the calm frames anchor the baseline low.
    """
    if len(frames) < 2:
        return 0.0
    diffs = [_structural_diff(frames[i - 1], frames[i]) for i in range(1, len(frames))]
    baseline = min(diffs)
    excess = max(d - baseline for d in diffs)
    return _clamp01(excess / _MORPH_FULL)


def limb_spike_score(frames: Sequence[Gray]) -> float:
    """Robust-z of the worst edge-energy outlier, normalized to a 0..1 artifact.

    A single frame whose edge density spikes far above the others (an extra
    arm/finger appearing for a beat) shows up as a large robust-z in the edge-energy
    series. With too few frames or no spread, returns 0.
    """
    if len(frames) < 3:
        return 0.0
    energies = [_edge_energy(f) for f in frames]
    med = _median(energies)
    mad = _median([abs(e - med) for e in energies])
    scale = max(mad, _LIMB_SCALE_FLOOR)
    worst_z = max(0.6745 * abs(e - med) / scale for e in energies)
    return _clamp01(worst_z / _LIMB_FULL_Z)


def temporal_coherence(frames: Sequence[Gray]) -> TemporalReport:
    """Score flicker + morph + limb-spike and combine into one report (pure)."""
    flicker = round(flicker_score(frames), 4)
    morph = round(morph_score(frames), 4)
    limb = round(limb_spike_score(frames), 4)
    artifact = _clamp01(_W_FLICKER * flicker + _W_MORPH * morph + _W_LIMB * limb)
    return TemporalReport(
        flicker=flicker,
        morph=morph,
        limb_spike=limb,
        motion_artifact=round(artifact, 4),
        temporal=round(1.0 - artifact, 4),
        n_frames=len(frames),
    )


def frames_to_gray(images: Sequence[bytes], *, max_dim: int = 64) -> list[Gray]:
    """Decode PNG/JPEG frame ``bytes`` to small grayscale grids for the analysis.

    Downsamples to at most ``max_dim`` on the long edge — the temporal signals are
    global/structural, so a thumbnail is plenty and keeps the pure math fast. Returns
    an empty list (no crash) if Pillow is unavailable or a frame fails to decode, so
    the caller can fall back to the VL motion rating.
    """
    try:
        import io

        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dep; defensive only
        return []
    grids: list[Gray] = []
    for data in images:
        try:
            img = Image.open(io.BytesIO(data)).convert("L")
        except Exception:
            continue
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        px = list(img.getdata())
        cols = img.size[0]
        grid = [[px[r * cols + c] / 255.0 for c in range(cols)] for r in range(img.size[1])]
        grids.append(grid)
    return grids


__all__ = [
    "Gray",
    "TemporalReport",
    "flicker_score",
    "frames_to_gray",
    "limb_spike_score",
    "morph_score",
    "temporal_coherence",
]
