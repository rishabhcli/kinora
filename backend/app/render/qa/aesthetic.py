"""Perceptual / aesthetic quality scoring (a new QA axis beyond §9.5's four checks).

The §9.5 checks catch *inconsistency* (wrong face, drifted style, contradicted
timeline, broken motion) but not *ugliness*: a clip can be perfectly consistent and
still be a blurry, blown-out, washed-out, or muddy mess. This module adds cheap,
deterministic perceptual proxies that approximate "is this technically a good-looking
frame" — the kind of no-reference image-quality features that correlate with human
aesthetic judgement:

* **sharpness** — gradient energy (a blurry frame has little high-frequency detail);
* **exposure** — penalizes clipped shadows/highlights (a frame that is mostly pure
  black or pure white has lost information);
* **contrast** — luminance spread (a flat, low-contrast frame reads as washed out);
* **colorfulness** — Hasler–Süsstrunk colorfulness on the RGB channels (a desaturated
  / muddy frame scores low) — the one signal that needs colour, so it is computed
  from the RGB grids while the others use luminance.

Each is mapped to a 0..1 *goodness* score and blended into one ``aesthetic`` score
that feeds the learned reward as a soft axis (never a hard gate — beauty is not
pre-registered, so it must not block a consistent clip). All math is pure over
already-decoded grids.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

Gray = list[list[float]]
#: An RGB frame: rows of (r, g, b) triples in 0..1.
Rgb = list[list[tuple[float, float, float]]]

#: Blend weights for the four sub-scores (sum to 1.0).
_W_SHARP, _W_EXPOSURE, _W_CONTRAST, _W_COLOR = 0.35, 0.2, 0.25, 0.2
#: Gradient energy at/above this is treated as "fully sharp".
_SHARP_FULL = 0.08
#: Fraction of pixels clipped (near 0 or near 1) at/above which exposure is "ruined".
_CLIP_FULL = 0.35
#: Luminance std at/above this is "full contrast".
_CONTRAST_FULL = 0.22
#: Hasler–Süsstrunk colorfulness at/above this is "fully colorful".
_COLOR_FULL = 0.5


@dataclass(frozen=True, slots=True)
class AestheticReport:
    """The perceptual-quality verdict for one clip (mean over its frames)."""

    sharpness: float = 1.0
    exposure: float = 1.0
    contrast: float = 1.0
    colorfulness: float = 1.0
    #: Blended 0..1 aesthetic goodness (1 = great-looking).
    aesthetic: float = 1.0
    n_frames: int = 0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _to_gray(frame: Rgb) -> Gray:
    return [[0.299 * r + 0.587 * g + 0.114 * b for (r, g, b) in row] for row in frame]


def _gradient_energy(gray: Gray) -> float:
    total = 0.0
    count = 0
    rows = len(gray)
    for y in range(rows):
        cols = len(gray[y])
        for x in range(cols):
            if x + 1 < cols:
                total += abs(gray[y][x + 1] - gray[y][x])
                count += 1
            if y + 1 < rows:
                total += abs(gray[y + 1][x] - gray[y][x])
                count += 1
    return total / count if count else 0.0


def sharpness_score(gray: Gray) -> float:
    """0..1 sharpness from gradient energy (a blurry frame has little detail)."""
    return _clamp01(_gradient_energy(gray) / _SHARP_FULL)


def exposure_score(gray: Gray) -> float:
    """0..1 exposure goodness — penalizes clipped shadows/highlights."""
    flat = [v for row in gray for v in row]
    if not flat:
        return 1.0
    clipped = sum(1 for v in flat if v < 0.02 or v > 0.98)
    frac = clipped / len(flat)
    return _clamp01(1.0 - frac / _CLIP_FULL)


def contrast_score(gray: Gray) -> float:
    """0..1 contrast from luminance standard deviation (flat frame = washed out)."""
    flat = [v for row in gray for v in row]
    if not flat:
        return 1.0
    mu = _mean(flat)
    var = _mean([(v - mu) ** 2 for v in flat])
    std = var**0.5
    return _clamp01(std / _CONTRAST_FULL)


def colorfulness_score(frame: Rgb) -> float:
    """0..1 Hasler–Süsstrunk colorfulness (a desaturated / muddy frame scores low)."""
    rg: list[float] = []
    yb: list[float] = []
    for row in frame:
        for r, g, b in row:
            rg.append(r - g)
            yb.append(0.5 * (r + g) - b)
    if not rg:
        return 1.0
    mu_rg, mu_yb = _mean(rg), _mean(yb)
    std_rg = _mean([(v - mu_rg) ** 2 for v in rg]) ** 0.5
    std_yb = _mean([(v - mu_yb) ** 2 for v in yb]) ** 0.5
    std_root = (std_rg**2 + std_yb**2) ** 0.5
    mean_root = (mu_rg**2 + mu_yb**2) ** 0.5
    colorfulness = std_root + 0.3 * mean_root
    return _clamp01(colorfulness / _COLOR_FULL)


def aesthetic_score(frames: Sequence[Rgb]) -> AestheticReport:
    """Mean perceptual quality across a clip's RGB frames (pure)."""
    if not frames:
        return AestheticReport()
    grays = [_to_gray(f) for f in frames]
    sharp = _mean([sharpness_score(g) for g in grays])
    expo = _mean([exposure_score(g) for g in grays])
    contr = _mean([contrast_score(g) for g in grays])
    color = _mean([colorfulness_score(f) for f in frames])
    blended = _W_SHARP * sharp + _W_EXPOSURE * expo + _W_CONTRAST * contr + _W_COLOR * color
    return AestheticReport(
        sharpness=round(sharp, 4),
        exposure=round(expo, 4),
        contrast=round(contr, 4),
        colorfulness=round(color, 4),
        aesthetic=round(_clamp01(blended), 4),
        n_frames=len(frames),
    )


def frames_to_rgb(images: Sequence[bytes], *, max_dim: int = 64) -> list[Rgb]:
    """Decode PNG/JPEG frame ``bytes`` to small RGB grids for the analysis.

    Mirrors :func:`app.render.qa.temporal.frames_to_gray` but keeps colour (the
    colorfulness signal needs it). Returns an empty list on any decode failure so the
    caller falls back to a neutral aesthetic.
    """
    try:
        import io

        from PIL import Image
    except Exception:  # pragma: no cover - defensive
        return []
    grids: list[Rgb] = []
    for data in images:
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except Exception:
            continue
        w, h = img.size
        if max(w, h) > max_dim:
            scale = max_dim / float(max(w, h))
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        px = list(img.getdata())
        cols = img.size[0]
        grid: Rgb = [
            [
                (
                    px[r * cols + c][0] / 255.0,
                    px[r * cols + c][1] / 255.0,
                    px[r * cols + c][2] / 255.0,
                )
                for c in range(cols)
            ]
            for r in range(img.size[1])
        ]
        grids.append(grid)
    return grids


__all__ = [
    "AestheticReport",
    "Rgb",
    "aesthetic_score",
    "colorfulness_score",
    "contrast_score",
    "exposure_score",
    "frames_to_rgb",
    "sharpness_score",
]
