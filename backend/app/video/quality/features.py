"""Frame-feature extraction — the :class:`FrameFeatureExtractor` seam + a pure impl.

A clip is scored from a small set of sampled frames. *What* features we read from
those frames is hidden behind the :class:`FrameFeatureExtractor` protocol so the rest
of the harness is model-agnostic and infra-free:

* in production a real extractor decodes the clip's frames (Pillow / ffmpeg) and
  computes the stats;
* in tests a :class:`StaticFeatureExtractor` returns hand-built numbers, so every
  sub-score and the aggregation are deterministic with **no decoding, no network**.

The default :class:`FrameStatsExtractor` is itself pure: it takes already-decoded
grayscale + RGB grids (the same ``Gray`` / ``Rgb`` shape the §9.5 ``render.qa``
modules use) and computes **no-reference** technical-integrity proxies that flag the
classic generated-video defects:

* **blockiness** — energy on the 8-px DCT-block grid vs off-grid (compression /
  tiling artifacts pulse on the macroblock lattice);
* **blur** — lack of high-frequency gradient energy (a soft, out-of-focus frame);
* **banding** — posterization: too few distinct luminance levels / flat plateaus
  (gradients collapse into visible steps);
* **temporal_flicker** — frame-to-frame global-luminance instability (the diffusion
  strobe), reusing the §9.5 *concept* without importing the Critic.

Each defect is a 0..1 *badness* score; ``technical_integrity`` is ``1 − blend`` so it
slots straight into :class:`~app.video.quality.scores.SubScores`. The grids carry no
Pillow dependency, so the math unit-tests on tiny grids.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

#: A grayscale frame: rows of luminances in 0..1 (matches ``render.qa`` ``Gray``).
Gray = list[list[float]]
#: An RGB frame: rows of (r, g, b) triples in 0..1 (matches ``render.qa`` ``Rgb``).
Rgb = list[list[tuple[float, float, float]]]

# --- defect → badness normalizers (a defect at/above the "full" level reads as 1.0) #
_BLOCK_FULL = 0.06
_BLUR_FULL_GRAD = 0.06  # gradient energy at/below ~0 is fully blurry; at/above is sharp
_BAND_FULL_LEVELS = 6  # <= this many distinct luminance levels reads as full banding
_FLICKER_FULL = 0.30
#: Blend weights for the four technical-integrity defects (sum to 1.0).
_W_BLOCK, _W_BLUR, _W_BAND, _W_FLICKER = 0.3, 0.3, 0.15, 0.25


def _clamp01(value: float) -> float:
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, value))


def _mean(values: Sequence[float]) -> float:
    return math.fsum(values) / len(values) if values else 0.0


def _frame_mean(frame: Gray) -> float:
    return _mean([v for row in frame for v in row])


@dataclass(frozen=True, slots=True)
class FrameFeatures:
    """The model-agnostic per-clip frame statistics consumed by the evaluator.

    All defect fields are 0..1 *badness* (0 = clean); ``motion_amount`` is a 0..1
    estimate of how much the clip actually moves (so the evaluator can reward
    *present-but-natural* motion and penalise a frozen clip). ``brightness`` and
    ``edge_energy`` are descriptive only.
    """

    n_frames: int = 0
    blockiness: float = 0.0
    blur: float = 0.0
    banding: float = 0.0
    temporal_flicker: float = 0.0
    motion_amount: float = 0.0
    brightness: float = 0.5
    edge_energy: float = 0.0
    extras: dict[str, float] = field(default_factory=dict)

    def technical_integrity(self) -> float:
        """Blend the four defect badnesses into a 0..1 *goodness* (1 = pristine)."""
        badness = (
            _W_BLOCK * self.blockiness
            + _W_BLUR * self.blur
            + _W_BAND * self.banding
            + _W_FLICKER * self.temporal_flicker
        )
        return round(_clamp01(1.0 - badness), 6)


@runtime_checkable
class FrameFeatureExtractor(Protocol):
    """Seam: turn one clip's sampled frames into model-agnostic :class:`FrameFeatures`.

    Real impls decode the clip; tests inject canned features. Synchronous + pure by
    contract (decoding side effects, if any, are the impl's business).
    """

    def extract(self, gray: Sequence[Gray], rgb: Sequence[Rgb]) -> FrameFeatures:
        """Compute features from aligned grayscale + RGB frame grids."""
        ...


# --------------------------------------------------------------------------- #
# Pure no-reference defect proxies (operate on tiny grids, no Pillow).
# --------------------------------------------------------------------------- #
def blockiness_score(frame: Gray, *, block: int = 8) -> float:
    """0..1 badness: extra discontinuity energy on the ``block``-px macroblock grid.

    Compares the mean absolute luminance step *across* block boundaries to the mean
    step *within* blocks; a clean frame has no preference for the lattice, a
    compressed/tiled one spikes on it. Frames smaller than one block return 0.
    """
    rows = len(frame)
    if rows == 0:
        return 0.0
    cols = len(frame[0])
    if rows < block + 1 and cols < block + 1:
        return 0.0
    on_grid: list[float] = []
    off_grid: list[float] = []
    # vertical boundaries (column steps)
    for y in range(rows):
        row = frame[y]
        for x in range(1, len(row)):
            step = abs(row[x] - row[x - 1])
            (on_grid if x % block == 0 else off_grid).append(step)
    # horizontal boundaries (row steps)
    for y in range(1, rows):
        for x in range(min(cols, len(frame[y]), len(frame[y - 1]))):
            step = abs(frame[y][x] - frame[y - 1][x])
            (on_grid if y % block == 0 else off_grid).append(step)
    if not on_grid or not off_grid:
        return 0.0
    excess = _mean(on_grid) - _mean(off_grid)
    return _clamp01(excess / _BLOCK_FULL)


def _gradient_energy(frame: Gray) -> float:
    """Mean local gradient magnitude (horizontal + vertical first differences)."""
    total = 0.0
    count = 0
    rows = len(frame)
    for y in range(rows):
        row = frame[y]
        cols = len(row)
        for x in range(cols):
            if x + 1 < cols:
                total += abs(row[x + 1] - row[x])
                count += 1
            if y + 1 < rows and x < len(frame[y + 1]):
                total += abs(frame[y + 1][x] - row[x])
                count += 1
    return total / count if count else 0.0


def blur_score(frame: Gray) -> float:
    """0..1 badness: lack of high-frequency detail (low gradient energy → blurry)."""
    grad = _gradient_energy(frame)
    sharpness = _clamp01(grad / _BLUR_FULL_GRAD)
    return _clamp01(1.0 - sharpness)


def banding_score(frame: Gray, *, levels: int = 32) -> float:
    """0..1 badness: posterization — few distinct quantized luminance levels.

    Quantizes to ``levels`` bins and counts how many are populated; a smooth photo
    fills many, a banded gradient collapses into a handful. Maps the distinct-level
    count below ``_BAND_FULL_LEVELS`` toward full banding.
    """
    flat = [v for row in frame for v in row]
    if not flat:
        return 0.0
    seen: set[int] = set()
    for v in flat:
        seen.add(min(levels - 1, int(_clamp01(v) * levels)))
    distinct = len(seen)
    # Ramp from 0 badness at >= 2*FULL distinct levels up to 1.0 at <= FULL levels.
    full = max(1, _BAND_FULL_LEVELS)
    if distinct >= 2 * full:
        return 0.0
    if distinct <= full:
        return 1.0
    return _clamp01((2 * full - distinct) / full)


def temporal_flicker_score(frames: Sequence[Gray]) -> float:
    """0..1 badness: max frame-to-frame global-luminance jump (the §9.5 strobe)."""
    if len(frames) < 2:
        return 0.0
    means = [_frame_mean(f) for f in frames]
    jumps = [abs(means[i] - means[i - 1]) for i in range(1, len(means))]
    return _clamp01(max(jumps) / _FLICKER_FULL)


def _structural_diff(a: Gray, b: Gray) -> float:
    total = 0.0
    count = 0
    for ra, rb in zip(a, b, strict=False):
        for va, vb in zip(ra, rb, strict=False):
            total += abs(va - vb)
            count += 1
    return total / count if count else 0.0


def motion_amount_score(frames: Sequence[Gray]) -> float:
    """0..1 estimate of how much the clip moves (mean frame-to-frame structural Δ).

    Not a defect — fed to the evaluator so a frozen clip (≈0) and a chaotic one
    (≈1) both read as *unnatural* amounts, while a moderate amount reads as natural.
    """
    if len(frames) < 2:
        return 0.0
    diffs = [_structural_diff(frames[i], frames[i - 1]) for i in range(1, len(frames))]
    # ~0.15 mean abs Δ already reads as a lot of motion in 0..1 luminance space.
    return _clamp01(_mean(diffs) / 0.15)


@dataclass(frozen=True, slots=True)
class FrameStatsExtractor:
    """Default pure extractor: no-reference defect proxies over decoded grids.

    Stateless; safe to share. Decoding ``bytes`` → grids is the caller's job (see
    ``render.qa`` ``frames_to_gray`` / ``frames_to_rgb`` for the production decoder);
    this class only does the math, so it unit-tests on tiny hand-built grids.
    """

    def extract(self, gray: Sequence[Gray], rgb: Sequence[Rgb]) -> FrameFeatures:  # noqa: ARG002
        if not gray:
            return FrameFeatures(n_frames=0)
        blockiness = _mean([blockiness_score(f) for f in gray])
        blur = _mean([blur_score(f) for f in gray])
        banding = _mean([banding_score(f) for f in gray])
        flicker = temporal_flicker_score(gray)
        motion = motion_amount_score(gray)
        brightness = _mean([_frame_mean(f) for f in gray])
        edge = _mean([_gradient_energy(f) for f in gray])
        return FrameFeatures(
            n_frames=len(gray),
            blockiness=round(blockiness, 6),
            blur=round(blur, 6),
            banding=round(banding, 6),
            temporal_flicker=round(flicker, 6),
            motion_amount=round(motion, 6),
            brightness=round(brightness, 6),
            edge_energy=round(edge, 6),
        )


@dataclass(frozen=True, slots=True)
class StaticFeatureExtractor:
    """Test seam: return canned :class:`FrameFeatures` ignoring the input frames."""

    features: FrameFeatures

    def extract(self, gray: Sequence[Gray], rgb: Sequence[Rgb]) -> FrameFeatures:  # noqa: ARG002
        return self.features
