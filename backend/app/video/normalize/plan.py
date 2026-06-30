"""The PURE plan layer: build exact ffmpeg arg lists without ever running ffmpeg.

Every transcode this subsystem performs is first expressed as a deterministic
``list[str]`` of ffmpeg arguments (or a ``-vf`` / ``-filter_complex`` chain).
Separating *what to run* from *running it* makes the entire decision surface —
which scale/pad/crop filter, which colour tags, whether loudness is applied, how
clips concatenate — unit-testable with zero subprocess calls, and keeps the
:class:`~app.video.normalize.normalizer.Normalizer` a thin executor.

Filenames are passed in by the executor (it owns the temp dir), so these helpers
stay free of any filesystem side effect. The video-filter builders take a typed
:class:`~app.video.normalize.media_info.MediaInfo` (the probe result) and a
:class:`~app.video.normalize.targets.NormalizationTarget`, and return both the
filter string and the planned output geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .aspect import plan_crop_fit, plan_pad_fit
from .media_info import MediaInfo
from .targets import AspectStrategy, ColorTags, LoudnessTarget, NormalizationTarget

#: ffprobe ``color_range`` → the ffmpeg ``scale``/``setrange`` token.
_RANGE_TO_FFMPEG = {"tv": "tv", "pc": "pc"}


@dataclass(frozen=True, slots=True)
class VideoFilterPlan:
    """A planned ``-vf`` chain plus the geometry it produces."""

    vf: str
    out_width: int
    out_height: int


@dataclass(frozen=True, slots=True)
class NormalizePlan:
    """The complete planned ffmpeg invocation for normalising one clip."""

    args: list[str]
    out_width: int
    out_height: int
    #: ``True`` when a silent audio track was synthesised to give the clip a
    #: uniform stereo layout (a provider clip with no audio).
    synthesized_audio: bool
    filtergraph: str = ""


# --------------------------------------------------------------------------- #
# Video filter chain (scale / pad / crop / fps / pixfmt / colour)
# --------------------------------------------------------------------------- #


def _geometry_filter(info: MediaInfo, target: NormalizationTarget) -> tuple[str, int, int]:
    """Build the scale(+pad|+crop) fragment and report the resulting geometry.

    Uses the *display* dimensions (rotation-corrected) when known; when the source
    geometry is unknown (an un-probable clip) it scales straight to the target with
    aspect forced to ``decrease`` + a pad, which is safe for any input.
    """
    tgt_w, tgt_h = target.dimensions
    dims = info.dimensions

    if target.aspect is AspectStrategy.STRETCH:
        return (f"scale={tgt_w}:{tgt_h}", tgt_w, tgt_h)

    if target.aspect is AspectStrategy.NONE:
        # Preserve aspect, do not force exact target dims (codec/fps-only fix).
        return (
            f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease",
            tgt_w,
            tgt_h,
        )

    if dims is None:
        # Unknown source geometry: decrease-fit + centre pad always lands on target.
        chain = (
            f"scale={tgt_w}:{tgt_h}:force_original_aspect_ratio=decrease,"
            f"pad={tgt_w}:{tgt_h}:(ow-iw)/2:(oh-ih)/2:color={target.pad_color}"
        )
        return (chain, tgt_w, tgt_h)

    if target.aspect is AspectStrategy.CROP:
        crop = plan_crop_fit(dims, (tgt_w, tgt_h), focal=target.focal_point)
        chain = f"scale={crop.scaled_w}:{crop.scaled_h}"
        if crop.needs_crop:
            chain += f",crop={tgt_w}:{tgt_h}:{crop.crop_x}:{crop.crop_y}"
        return (chain, tgt_w, tgt_h)

    # PAD (default): scale-inside + centre pad.
    pad = plan_pad_fit(dims, (tgt_w, tgt_h))
    chain = f"scale={pad.scaled_w}:{pad.scaled_h}"
    if pad.needs_pad:
        chain += f",pad={tgt_w}:{tgt_h}:{pad.pad_x}:{pad.pad_y}:color={target.pad_color}"
    return (chain, tgt_w, tgt_h)


def _color_filter(color: ColorTags) -> str | None:
    """A ``setrange`` fragment for the canonical colour range (tags ride -bsf/-x264opts).

    The range must be *converted* in the pixel pipeline (``setrange``) — purely
    tagging it would leave the actual sample values inconsistent — while the
    primaries/transfer/space are metadata stamped by the encoder flags
    (:func:`color_metadata_args`).
    """
    if color.range and color.range in _RANGE_TO_FFMPEG:
        return f"setrange={_RANGE_TO_FFMPEG[color.range]}"
    return None


def build_video_filter(info: MediaInfo, target: NormalizationTarget) -> VideoFilterPlan:
    """Compose the full ordered ``-vf`` chain for one clip → the canonical target.

    Order matters: geometry first (scale/pad/crop), then fps, then a forced SAR of
    1 (anamorphic sources otherwise stay non-square), then the canonical pixel
    format, then the colour-range conversion. The whole chain is deterministic so
    the same clip + target always plans byte-identical args.
    """
    geometry, out_w, out_h = _geometry_filter(info, target)
    parts = [geometry, f"fps={target.fps}", "setsar=1", f"format={target.pixel_format}"]
    color = _color_filter(target.color)
    if color:
        parts.append(color)
    return VideoFilterPlan(vf=",".join(parts), out_width=out_w, out_height=out_h)


def color_metadata_args(color: ColorTags) -> list[str]:
    """Encoder args that *stamp* the canonical colour primaries/transfer/space.

    Applied alongside the encode so every output mp4 advertises one colour profile
    in its stream metadata (the bars/levels were already converted by the filter
    chain). Empty when no colour tags are configured.
    """
    args: list[str] = []
    if color.primaries:
        args += ["-color_primaries", color.primaries]
    if color.transfer:
        args += ["-color_trc", color.transfer]
    if color.space:
        args += ["-colorspace", color.space]
    if color.range:
        args += ["-color_range", color.range]
    return args


def loudnorm_filter(loudness: LoudnessTarget) -> str | None:
    """An EBU R128 ``loudnorm`` audio-filter fragment, or ``None`` when disabled."""
    if not loudness.enabled:
        return None
    return (
        f"loudnorm=I={loudness.integrated_lufs:g}:"
        f"TP={loudness.true_peak:g}:LRA={loudness.loudness_range:g}"
    )


# --------------------------------------------------------------------------- #
# Encoder arg tails (video + audio), shared by normalize & concat
# --------------------------------------------------------------------------- #


def video_encode_args(target: NormalizationTarget) -> list[str]:
    """The canonical video-encoder arg tail (codec, preset, crf, pixfmt, fps, colour)."""
    args = [
        "-c:v",
        target.video_codec,
        "-pix_fmt",
        target.pixel_format,
        "-r",
        str(target.fps),
    ]
    if target.video_codec in ("libx264", "libx265"):
        args += ["-preset", target.x264_preset, "-crf", str(target.crf)]
    args += color_metadata_args(target.color)
    return args


def audio_encode_args(target: NormalizationTarget) -> list[str]:
    """The canonical audio-encoder arg tail (codec, bitrate, sample-rate, channels)."""
    return [
        "-c:a",
        target.audio_codec,
        "-b:a",
        target.audio_bitrate,
        "-ar",
        str(target.audio_sample_rate),
        "-ac",
        str(target.audio_channels),
    ]


# --------------------------------------------------------------------------- #
# Full normalize invocation
# --------------------------------------------------------------------------- #


def build_normalize_args(
    *,
    ffmpeg: str,
    in_path: str,
    out_path: str,
    info: MediaInfo,
    target: NormalizationTarget,
) -> NormalizePlan:
    """Plan the full ffmpeg command that normalises one clip to ``target``.

    A clip with no audio gets a synthesised silent stereo track (matched to the
    target sample-rate/channels and trimmed to the video with ``-shortest``) so
    every normalised clip has the identical stream layout — the precondition for
    a clean downstream concat. When loudness normalisation is enabled it is applied
    to the audio via a per-input ``loudnorm`` pass.

    Returns a :class:`NormalizePlan` carrying the arg list, the output geometry,
    and whether a silent track was synthesised — the executor only runs the args.
    """
    vplan = build_video_filter(info, target)
    aud = loudnorm_filter(target.loudness)
    has_audio = info.has_audio

    args: list[str] = [ffmpeg, "-y", "-i", in_path]
    filter_parts: list[str] = [f"[0:v]{vplan.vf}[v]"]
    map_args: list[str] = ["-map", "[v]"]
    synthesized = False

    if has_audio:
        if aud:
            filter_parts.append(f"[0:a]{aud}[a]")
            map_args += ["-map", "[a]"]
        else:
            map_args += ["-map", "0:a:0"]
    else:
        synthesized = True
        # Synthesise silence on a second lavfi input so the layout is uniform.
        layout = "stereo" if target.audio_channels == 2 else f"{target.audio_channels}c"
        args += [
            "-f",
            "lavfi",
            "-i",
            f"anullsrc=channel_layout={layout}:sample_rate={target.audio_sample_rate}",
        ]
        if aud:
            filter_parts.append(f"[1:a]{aud}[a]")
            map_args += ["-map", "[a]"]
        else:
            map_args += ["-map", "1:a:0"]
        map_args.append("-shortest")

    args += ["-filter_complex", ";".join(filter_parts), *map_args]
    args += video_encode_args(target)
    args += audio_encode_args(target)
    args += ["-movflags", "+faststart", out_path]

    return NormalizePlan(
        args=args,
        out_width=vplan.out_width,
        out_height=vplan.out_height,
        synthesized_audio=synthesized,
        filtergraph=";".join(filter_parts),
    )


# --------------------------------------------------------------------------- #
# Last-frame extraction (universal — any container)
# --------------------------------------------------------------------------- #


def build_last_frame_args(
    *,
    ffmpeg: str,
    in_path: str,
    out_path: str,
    duration_s: float | None = None,
    seek_back_s: float = 0.05,
) -> list[str]:
    """Plan a command that extracts the *last* decodable frame as a still image.

    The robust universal recipe (works for every container, incl. VFR / odd GOP
    sources from mixed providers): decode the whole stream and keep only the final
    frame (``-update 1`` overwrites the single output so the last write wins). When
    a duration is known we additionally fast-seek to just before the end first, so
    a long clip does not decode in full. The output extension (``.png``/``.jpg``)
    selects the codec via ffmpeg's muxer auto-detection.
    """
    args = [ffmpeg, "-y"]
    if duration_s and duration_s > seek_back_s:
        # Input-seek to near the end (fast), then take the last frame of the tail.
        args += ["-sseof", f"-{max(seek_back_s, 0.04):.3f}"]
    args += ["-i", in_path, "-update", "1", "-frames:v", "1"]
    # ``-q:v 2`` keeps a high-quality still when the muxer is JPEG; harmless for PNG.
    args += ["-q:v", "2", out_path]
    return args


def build_last_frame_fallback_args(
    *,
    ffmpeg: str,
    in_path: str,
    out_path: str,
) -> list[str]:
    """A no-seek fallback for the last frame: full decode keeping only the final.

    Some containers report no/zero duration so the ``-sseof`` seek in
    :func:`build_last_frame_args` lands nowhere; this decodes the whole clip and
    keeps the last frame written. Slower but works on any decodable input.
    """
    return [ffmpeg, "-y", "-i", in_path, "-update", "1", "-frames:v", "1", "-q:v", "2", out_path]


# --------------------------------------------------------------------------- #
# Concat (re-encode, with a stream-copy fast path the executor may choose)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ConcatPlan:
    """A planned concat: the args + whether each input needed re-normalising."""

    args: list[str]
    reencoded: bool
    filtergraph: str = ""
    inputs_uniform: bool = field(default=False)


def streams_are_uniform(infos: list[MediaInfo]) -> bool:
    """True when every clip shares geometry / fps / codec / pixfmt / audio layout.

    This is the precondition for the cheap demuxer concat (stream copy, no
    re-encode). Any disagreement (the normal mixed-provider case) means the
    executor must fall back to the filter-graph re-encode concat.
    """
    if len(infos) <= 1:
        return True
    first = infos[0]
    fv = first.video
    fa = first.audio
    if fv is None:
        return False
    for other in infos[1:]:
        ov = other.video
        if ov is None:
            return False
        if first.dimensions != other.dimensions:
            return False
        if (fv.fps is None or ov.fps is None) or abs(fv.fps - ov.fps) > 0.01:
            return False
        if fv.codec_name != ov.codec_name or fv.pixel_format != ov.pixel_format:
            return False
        oa = other.audio
        if (fa is None) != (oa is None):
            return False
        if fa is not None and oa is not None:
            if fa.codec_name != oa.codec_name or fa.sample_rate != oa.sample_rate:
                return False
            if fa.channels != oa.channels:
                return False
    return True


def build_concat_demux_args(
    *,
    ffmpeg: str,
    list_path: str,
    out_path: str,
) -> list[str]:
    """Plan the cheap demuxer (stream-copy) concat — only valid for uniform inputs.

    Reads a concat-demuxer manifest file (``file '...'`` lines the executor wrote)
    and copies both streams with no re-encode. The executor must have verified
    :func:`streams_are_uniform` first; otherwise the join will glitch.
    """
    return [
        ffmpeg,
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_path,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        out_path,
    ]


def build_concat_reencode_args(
    *,
    ffmpeg: str,
    in_paths: list[str],
    out_path: str,
    target: NormalizationTarget,
) -> ConcatPlan:
    """Plan the robust filter-graph concat (re-encode) for non-uniform inputs.

    Each input is fed to the ``concat`` filter (``v=1:a=1``); the executor is
    responsible for having normalised every input to the target *first* so the
    filter's "all inputs must share parameters" rule holds. The audio is level-
    normalised with ``dynaudnorm`` so a quiet clip never jumps a loud one — the
    same join policy :mod:`app.render.stitch` uses, kept here for parity.
    """
    n = len(in_paths)
    args: list[str] = [ffmpeg, "-y"]
    for path in in_paths:
        args += ["-i", path]
    streams = "".join(f"[{i}:v:0][{i}:a:0]" for i in range(n))
    graph = f"{streams}concat=n={n}:v=1:a=1[v][araw];[araw]dynaudnorm[a]"
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]"]
    args += video_encode_args(target)
    args += audio_encode_args(target)
    args += ["-movflags", "+faststart", out_path]
    return ConcatPlan(args=args, reencoded=True, filtergraph=graph)


__all__ = [
    "ConcatPlan",
    "NormalizePlan",
    "VideoFilterPlan",
    "audio_encode_args",
    "build_concat_demux_args",
    "build_concat_reencode_args",
    "build_last_frame_args",
    "build_last_frame_fallback_args",
    "build_normalize_args",
    "build_video_filter",
    "color_metadata_args",
    "loudnorm_filter",
    "streams_are_uniform",
    "video_encode_args",
]
