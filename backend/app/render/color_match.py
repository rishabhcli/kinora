"""Shot-to-shot color matching for the scene stitch (§9.6).

A scene can stitch a full Wan clip next to a degraded Ken-Burns rung; their exposure
and white balance rarely match, so the cut "flickers" in brightness/colour. This
module derives a **gentle per-clip grade** that nudges each clip toward a scene
**reference** (by convention the first clip — usually the highest-quality / first
accepted shot), so the stitched scene reads as one continuous look.

The colour statistics are real (ffmpeg ``signalstats`` over a clip → mean luma +
per-channel means via the YUV→approx-RGB the filter reports). The **correction
derivation is pure**: given two stat blocks it computes a clamped brightness +
white-balance correction. So :func:`derive_correction` / :func:`grade_filter` are
unit-tested without ffmpeg; only :func:`measure_stats` runs the binary.

Corrections are deliberately *gentle* and *clamped* — color matching is meant to
remove a jarring jump, not to repaint a clip. A clip already close to the reference
gets a near-identity (often no-op) grade.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.render.degrade import FfmpegError, get_ffmpeg_exe

logger = get_logger("app.render.color_match")

#: signalstats prints per-frame ``lavfi.signalstats.YAVG`` etc. to metadata; with
#: ``-f null`` + ``metadata=print`` it lands on stderr/stdout as ``key=value``.
_STAT_RE = re.compile(r"lavfi\.signalstats\.([A-Z]+)=([0-9.]+)")

#: Clamp bounds so a correction stays a nudge, never a repaint.
_MAX_BRIGHTNESS = 0.12  # ffmpeg eq brightness is roughly [-1, 1]; we cap well inside
_MAX_CHANNEL_GAIN = 0.18  # colorbalance midtone shift cap per channel


@dataclass(frozen=True, slots=True)
class ColorStats:
    """Representative colour statistics of a clip (means over sampled frames).

    Values are on signalstats' 0..255 scale. ``y`` is luma; ``u``/``v`` are the
    chroma means (128 = neutral). A clip's warmth/coolness shows in ``u``/``v``
    deviating from 128.
    """

    y: float
    u: float
    v: float

    @property
    def is_valid(self) -> bool:
        return self.y > 0.0


@dataclass(frozen=True, slots=True)
class ColorCorrection:
    """A gentle, clamped grade toward a reference (ffmpeg-filter parameters).

    Attributes:
        brightness: ``eq=brightness=`` delta (luma lift/cut), clamped.
        warm_shift: midtone red/blue balance shift; >0 warms, <0 cools, clamped.
    """

    brightness: float
    warm_shift: float

    @property
    def is_identity(self) -> bool:
        return abs(self.brightness) < 1e-4 and abs(self.warm_shift) < 1e-4


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def derive_correction(clip: ColorStats, reference: ColorStats) -> ColorCorrection:
    """Compute the clamped grade nudging ``clip`` toward ``reference`` (pure).

    * Brightness: the luma gap (reference − clip), scaled from the 0..255 domain into
      ffmpeg's eq-brightness domain and clamped to a gentle ceiling.
    * Warmth: the *difference of differences* of the V (red-ish) minus U (blue-ish)
      chroma relative to neutral — i.e. how much warmer/cooler the clip is than the
      reference — mapped to a clamped midtone colour-balance shift.

    Returns an identity correction when either stat block is invalid (no measurement)
    so a probe failure never repaints a clip.
    """
    if not clip.is_valid or not reference.is_valid:
        return ColorCorrection(brightness=0.0, warm_shift=0.0)
    # Luma gap → eq brightness (eq brightness ~[-1,1] maps the full 0..255 range).
    brightness = _clamp((reference.y - clip.y) / 255.0, _MAX_BRIGHTNESS)
    # Warmth proxy: (V-U) is high for warm images. Match the clip's warmth to the ref.
    clip_warm = clip.v - clip.u
    ref_warm = reference.v - reference.u
    warm_shift = _clamp((ref_warm - clip_warm) / 255.0, _MAX_CHANNEL_GAIN)
    return ColorCorrection(brightness=round(brightness, 5), warm_shift=round(warm_shift, 5))


def grade_filter(correction: ColorCorrection) -> str | None:
    """The ffmpeg filter chain for a correction, or ``None`` for an identity grade.

    Combines an ``eq`` brightness lift with a ``colorbalance`` midtone shift (warm =
    push red up / blue down). Gentle by construction (the correction was clamped).
    """
    if correction.is_identity:
        return None
    parts: list[str] = []
    if abs(correction.brightness) >= 1e-4:
        parts.append(f"eq=brightness={correction.brightness:.5f}")
    if abs(correction.warm_shift) >= 1e-4:
        rm = correction.warm_shift
        bm = -correction.warm_shift
        parts.append(f"colorbalance=rm={rm:.5f}:bm={bm:.5f}")
    return ",".join(parts) or None


def measure_stats(clip_bytes: bytes, *, sample_frames: int = 12) -> ColorStats:
    """Measure a clip's mean luma/chroma with ffmpeg ``signalstats`` (real probe).

    Samples up to ``sample_frames`` frames (a ``select`` over the clip), averages the
    per-frame YAVG/UAVG/VAVG signalstats. Returns an invalid (zeroed) stat block on
    any ffmpeg failure so the caller falls back to an identity grade.
    """
    if not clip_bytes:
        return ColorStats(0.0, 0.0, 0.0)
    try:
        ffmpeg = get_ffmpeg_exe()
    except FfmpegError:
        return ColorStats(0.0, 0.0, 0.0)
    with tempfile.TemporaryDirectory(prefix="kinora_colstat_") as tmp:
        path = Path(tmp) / "clip.mp4"
        path.write_bytes(clip_bytes)
        # Sample evenly, run signalstats, print its metadata; -f null discards video.
        vf = (
            f"select='not(mod(n,{max(1, 30 // max(1, sample_frames))}))',"
            "signalstats,metadata=print:file=-"
        )
        import subprocess

        proc = subprocess.run(  # noqa: S603 - resolved binary, fixed args
            [ffmpeg, "-hide_banner", "-i", str(path), "-vf", vf, "-an", "-f", "null", "-"],
            capture_output=True,
            timeout=120.0,
            check=False,
        )
    text = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
    sums: dict[str, float] = {"YAVG": 0.0, "UAVG": 0.0, "VAVG": 0.0}
    counts: dict[str, int] = {"YAVG": 0, "UAVG": 0, "VAVG": 0}
    for key, value in _STAT_RE.findall(text):
        if key in sums:
            sums[key] += float(value)
            counts[key] += 1
    if counts["YAVG"] == 0:
        return ColorStats(0.0, 0.0, 0.0)

    def _mean(key: str, default: float) -> float:
        return sums[key] / counts[key] if counts[key] else default

    return ColorStats(
        y=round(_mean("YAVG", 0.0), 3),
        u=round(_mean("UAVG", 128.0), 3),
        v=round(_mean("VAVG", 128.0), 3),
    )


def plan_scene_grades(clips: list[bytes]) -> list[ColorCorrection]:
    """Measure every clip and derive each one's grade toward the first (reference).

    The first clip is the reference and always gets an identity grade. A scene of
    one clip yields a single identity grade. Used by the stitch path to colour-match
    a whole scene before concatenation.
    """
    if not clips:
        return []
    stats = [measure_stats(clip) for clip in clips]
    reference = stats[0]
    grades = [ColorCorrection(0.0, 0.0)]
    for clip_stats in stats[1:]:
        grades.append(derive_correction(clip_stats, reference))
    logger.info(
        "color_match.plan",
        clips=len(clips),
        graded=sum(1 for g in grades if not g.is_identity),
    )
    return grades


__all__ = [
    "ColorCorrection",
    "ColorStats",
    "derive_correction",
    "grade_filter",
    "measure_stats",
    "plan_scene_grades",
]
