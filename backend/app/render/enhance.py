"""Frame-interpolation + upscaling hooks — make a cheap clip look richer (§9.2).

Wan *turbo* ids render short, lower-fps clips fast and cheap; *quality* ids are
slower and pricier. Rather than always pay for the quality id, a post-render
**enhancement** stage can smooth and sharpen a turbo clip with ffmpeg alone:

* **frame interpolation** (``minterpolate``) raises a low source fps to the film fps
  with motion-compensated in-between frames, and
* **upscale + sharpen** (``scale`` + ``unsharp``) lifts a sub-film-resolution clip up
  to :data:`~app.render.degrade.FILM_SIZE` with a gentle edge enhance.

This is a *hook*, not a mandate: the plan is computed deterministically from the
clip's probe + an :class:`EnhanceProfile`, and is a **no-op** when the clip already
meets the target (so a quality-id clip is passed straight through, zero work). It
costs **zero model spend** — it is pure ffmpeg over bytes you already have. The
``EnhancePlan`` is the seam a future GPU interpolator (RIFE/FILM, Phase 12) slots
behind: same plan, different executor.

Split like the rest of the render layer: :func:`plan_enhancement` is pure (probe
facts in, plan out) and unit-tested without ffmpeg; :func:`apply_enhancement` runs
ffmpeg and returns a real mp4 (skipped in tests when no binary).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.render.degrade import (
    DEFAULT_FPS,
    FILM_SIZE,
    FfmpegError,
    get_ffmpeg_exe,
    inspect,
    run_ffmpeg,
)

logger = get_logger("app.render.enhance")


@dataclass(frozen=True, slots=True)
class EnhanceProfile:
    """What "enhanced" means for a clip (deterministic target + thresholds).

    Attributes:
        target_fps: fps to interpolate up to (the film rate).
        target_size: (w, h) to upscale to (the vertical film geometry).
        sharpen_amount: ``unsharp`` luma amount applied when upscaling (0 = off).
        interpolate: enable motion-compensated frame interpolation.
        upscale: enable spatial upscale.
        min_fps_gain: only interpolate when the source is at least this many fps
            below target (interpolating a near-target clip is wasted work + risk).
        min_scale_gain: only upscale when the source area is below this fraction of
            the target area (avoid a pointless ~1× rescale).
    """

    target_fps: int = DEFAULT_FPS
    target_size: tuple[int, int] = FILM_SIZE
    sharpen_amount: float = 0.6
    interpolate: bool = True
    upscale: bool = True
    min_fps_gain: int = 6
    min_scale_gain: float = 0.85

    @classmethod
    def film_default(cls) -> EnhanceProfile:
        """The standard vertical-film enhancement target."""
        return cls()

    @classmethod
    def passthrough(cls) -> EnhanceProfile:
        """A profile that never changes a clip (both stages disabled)."""
        return cls(interpolate=False, upscale=False)


@dataclass(frozen=True, slots=True)
class EnhancePlan:
    """The resolved decision for one clip — what (if anything) to do.

    A plan with both flags ``False`` is a no-op; :func:`apply_enhancement` returns
    the clip unchanged for it (no ffmpeg run).
    """

    source_fps: float
    source_size: tuple[int, int]
    target_fps: int
    target_size: tuple[int, int]
    do_interpolate: bool
    do_upscale: bool
    sharpen_amount: float

    @property
    def is_noop(self) -> bool:
        return not self.do_interpolate and not self.do_upscale

    def video_filter(self) -> str | None:
        """The ffmpeg ``-vf`` chain for this plan, or ``None`` for a no-op."""
        if self.is_noop:
            return None
        chain: list[str] = []
        if self.do_interpolate:
            # Motion-compensated interpolation up to the target fps.
            chain.append(
                f"minterpolate=fps={self.target_fps}:mi_mode=mci:"
                "mc_mode=aobmc:me_mode=bidir:vsbmc=1"
            )
        if self.do_upscale:
            w, h = self.target_size
            chain.append(
                f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
            )
            if self.sharpen_amount > 0:
                chain.append(f"unsharp=5:5:{self.sharpen_amount:.3f}:5:5:0.0")
        chain.append("format=yuv420p")
        return ",".join(chain)


@dataclass(frozen=True, slots=True)
class SceneEnhanceResult:
    """An enhanced clip + the plan that produced it (telemetry / assertions)."""

    clip_bytes: bytes
    plan: EnhancePlan
    changed: bool


def _area(size: tuple[int, int]) -> int:
    return max(1, size[0]) * max(1, size[1])


def plan_enhancement(
    *,
    source_fps: float,
    source_size: tuple[int, int],
    profile: EnhanceProfile | None = None,
) -> EnhancePlan:
    """Decide what to do for a clip given its probed fps/size (pure).

    Interpolation is planned only when the source is meaningfully below the target
    fps (``min_fps_gain``); upscale only when the source area is below
    ``min_scale_gain`` of the target area. Either gate failing disables that stage,
    so a clip already at film fps/size yields a no-op plan.
    """
    prof = profile or EnhanceProfile.film_default()
    sw, sh = max(1, source_size[0]), max(1, source_size[1])
    do_interp = (
        prof.interpolate
        and source_fps > 0
        and (prof.target_fps - source_fps) >= prof.min_fps_gain
    )
    do_upscale = (
        prof.upscale and (_area((sw, sh)) / _area(prof.target_size)) < prof.min_scale_gain
    )
    return EnhancePlan(
        source_fps=round(source_fps, 3),
        source_size=(sw, sh),
        target_fps=prof.target_fps,
        target_size=prof.target_size,
        do_interpolate=do_interp,
        do_upscale=do_upscale,
        sharpen_amount=prof.sharpen_amount if do_upscale else 0.0,
    )


def plan_for_clip(clip_bytes: bytes, *, profile: EnhanceProfile | None = None) -> EnhancePlan:
    """Probe ``clip_bytes`` and plan its enhancement (pure decision, real probe)."""
    info = inspect(clip_bytes)
    fps = _fps_from_probe(info)
    size = (info.width or FILM_SIZE[0], info.height or FILM_SIZE[1])
    return plan_enhancement(source_fps=fps, source_size=size, profile=profile)


def _fps_from_probe(info: object) -> float:
    """Best-effort fps from a ProbeInfo's raw stream data (defaults to 0 = unknown).

    ffprobe reports ``avg_frame_rate`` like ``"24/1"``; the ffmpeg-stderr fallback
    has no fps, so we return 0 (which disables interpolation — never interpolate on
    a guess).
    """
    raw = getattr(info, "raw", {})
    streams = raw.get("streams", []) if isinstance(raw, dict) else []
    for stream in streams:
        if not isinstance(stream, dict) or stream.get("codec_type") != "video":
            continue
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = stream.get(key)
            if isinstance(value, str) and "/" in value:
                num, _, den = value.partition("/")
                try:
                    n, d = float(num), float(den)
                except ValueError:
                    continue
                if d > 0 and n > 0:
                    return n / d
    return 0.0


def apply_enhancement(
    clip_bytes: bytes,
    plan: EnhancePlan,
) -> SceneEnhanceResult:
    """Apply ``plan`` to ``clip_bytes`` with ffmpeg; a no-op plan returns it as-is.

    Audio is copied through untouched (enhancement is video-only). Returns a real,
    playable mp4.

    Raises:
        FfmpegError: when ffmpeg is unavailable or the filter run fails.
        ValueError: when ``clip_bytes`` is empty.
    """
    if not clip_bytes:
        raise ValueError("apply_enhancement: clip_bytes is empty")
    vf = plan.video_filter()
    if vf is None:
        return SceneEnhanceResult(clip_bytes=clip_bytes, plan=plan, changed=False)

    ffmpeg = get_ffmpeg_exe()
    has_audio = _safe_has_audio(clip_bytes)
    with tempfile.TemporaryDirectory(prefix="kinora_enhance_") as tmp:
        tmp_dir = Path(tmp)
        in_path = tmp_dir / "in.mp4"
        in_path.write_bytes(clip_bytes)
        out_path = tmp_dir / "out.mp4"
        args = [ffmpeg, "-y", "-i", str(in_path), "-vf", vf]
        args += [
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-r", str(plan.target_fps),
        ]
        if has_audio:
            args += ["-c:a", "copy"]
        else:
            args += ["-an"]
        args += ["-movflags", "+faststart", str(out_path)]
        run_ffmpeg(args)
        enhanced = out_path.read_bytes()

    logger.info(
        "enhance.apply",
        interpolate=plan.do_interpolate,
        upscale=plan.do_upscale,
        source_fps=plan.source_fps,
        target_fps=plan.target_fps,
        bytes=len(enhanced),
    )
    return SceneEnhanceResult(clip_bytes=enhanced, plan=plan, changed=True)


def enhance_clip(clip_bytes: bytes, *, profile: EnhanceProfile | None = None) -> SceneEnhanceResult:
    """Probe → plan → apply in one call (the pipeline entry point)."""
    return apply_enhancement(clip_bytes, plan_for_clip(clip_bytes, profile=profile))


def _safe_has_audio(clip: bytes) -> bool:
    try:
        return inspect(clip).has_audio
    except FfmpegError:
        return False


__all__ = [
    "EnhancePlan",
    "EnhanceProfile",
    "SceneEnhanceResult",
    "apply_enhancement",
    "enhance_clip",
    "plan_enhancement",
    "plan_for_clip",
]
