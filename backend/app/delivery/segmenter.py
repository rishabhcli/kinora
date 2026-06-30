"""The packaging *plan* layer — pure ffmpeg / segmenter arg-list builders.

This is the heart of the "fully unit-testable without ffmpeg" requirement: every
function here returns **exact argument lists** (and the manifest layer returns
exact manifest text) that *would* be handed to ffmpeg, but nothing executes.
:mod:`app.delivery.packager` is the thin layer that actually runs these plans.

It builds, for one :class:`~app.delivery.models.ShotClip` against one chosen
:class:`~app.delivery.ladder.Rendition`:

* a **normalize+encode** ffmpeg invocation that conforms the heterogeneous
  provider clip to the target grid (codec / fps / pixel format / closed GOP /
  forced keyframes at the segment cadence — see
  :func:`app.delivery.profiles.normalization_spec`), then
* a **CMAF/fMP4 segmentation** stage that fragments the encoded rendition into
  an init segment + numbered media segments whose boundaries fall on the forced
  IDR frames, emitting both an HLS media playlist and a DASH single-file index
  via ffmpeg's ``-f hls``/``dash`` muxers, *or* via a single ``-f mp4``
  fragmented output addressed by byte-range.

The two stages can be a single ffmpeg call (segmenting muxer) or a re-mux of an
already-normalized master; both arg builders are provided so the packager can
choose based on whether the master was pre-normalized.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.delivery.errors import SegmentationError
from app.delivery.ladder import Rendition
from app.delivery.models import ByteRange
from app.delivery.profiles import (
    TARGET_AUDIO_CODEC,
    NormalizationSpec,
)


class EncodePlan(BaseModel):
    """A fully-specified, executable-but-unexecuted ffmpeg encode invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: ``args[0]`` is a binary placeholder (``"ffmpeg"``); the packager substitutes
    #: the resolved binary. Keeping a placeholder makes the plan path-independent
    #: and golden-testable.
    args: list[str]
    output: str
    rendition_name: str
    full_transcode: bool

    def with_binary(self, ffmpeg_bin: str) -> list[str]:
        """Return the arg list with the real ffmpeg binary substituted in slot 0."""
        return [ffmpeg_bin, *self.args[1:]]


class SegmentationPlan(BaseModel):
    """A fully-specified CMAF segmentation invocation + its declared outputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    args: list[str]
    init_output: str
    segment_pattern: str
    media_playlist: str | None = None
    expected_segment_count: int = Field(ge=1)
    segment_durations: list[float]

    def with_binary(self, ffmpeg_bin: str) -> list[str]:
        return [ffmpeg_bin, *self.args[1:]]


def _video_filter(rendition: Rendition) -> str:
    """The scale+pad+fps filter conforming a source clip to a rendition's grid.

    ``scale=...:force_original_aspect_ratio=decrease`` fits within the box,
    ``pad`` letterboxes to the exact even dimensions, ``fps`` conforms the frame
    rate, ``setsar=1`` normalizes the pixel aspect. This is what makes a 25fps
    MiniMax clip and a 16fps Wan clip switch seamlessly inside one rendition.
    """
    w, h = rendition.width, rendition.height
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={rendition.fps},setsar=1"
    )


def build_encode_plan(
    *,
    source: str,
    rendition: Rendition,
    spec: NormalizationSpec,
    output: str,
) -> EncodePlan:
    """Build the ffmpeg arg list that normalizes ``source`` into one rendition mp4.

    The output is an intermediate single-file H.264 mp4 with a **closed GOP**,
    forced keyframes at the segment cadence, and the rendition's bitrate ladder
    (target + VBV peak + buffer). It is then handed to
    :func:`build_segmentation_plan` for fragmenting — or the two are fused via
    :func:`build_hls_segmenting_plan` / :func:`build_dash_segmenting_plan`.
    """
    gop = str(spec.gop_size)
    vmaxrate = f"{rendition.max_bitrate_kbps}k"
    vbufsize = f"{rendition.max_bitrate_kbps * 2}k"
    args = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        source,
        "-vf",
        _video_filter(rendition),
        "-c:v",
        "libx264",
        "-profile:v",
        rendition.profile,
        "-preset",
        "veryfast",
        "-pix_fmt",
        spec.pixel_format,
        "-b:v",
        f"{rendition.video_bitrate_kbps}k",
        "-maxrate",
        vmaxrate,
        "-bufsize",
        vbufsize,
        # Closed GOP + fixed cadence: IDR every ``gop`` frames, no scene-cut keys
        # before the boundary, and forced keys exactly on segment boundaries.
        "-g",
        gop,
        "-keyint_min",
        gop,
        "-sc_threshold",
        "0",
        "-force_key_frames",
        spec.force_keyframe_expr,
        "-c:a",
        TARGET_AUDIO_CODEC,
        "-b:a",
        f"{rendition.audio_bitrate_kbps}k",
        "-ar",
        str(spec.audio_sample_rate),
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        output,
    ]
    return EncodePlan(
        args=args,
        output=output,
        rendition_name=rendition.name,
        full_transcode=spec.full_transcode,
    )


def build_hls_segmenting_plan(
    *,
    source: str,
    rendition: Rendition,
    spec: NormalizationSpec,
    segment_durations: Sequence[float],
    segment_dir: str,
    init_name: str = "init.mp4",
    segment_template: str = "seg_%05d.m4s",
    media_playlist_name: str = "media.m3u8",
) -> SegmentationPlan:
    """Build a fused normalize+CMAF-HLS-segment ffmpeg invocation.

    Uses ffmpeg's ``-f hls`` muxer in ``fmp4`` segment mode: it emits a CMAF init
    segment, ``%05d``-numbered ``.m4s`` fragments, and the media playlist in one
    pass. The expected segment count + per-segment durations are carried so the
    manifest layer can assert the muxer produced what the plan promised.
    """
    if not segment_durations:
        raise SegmentationError("segment_durations must be non-empty")
    seg_time = f"{spec.segment_duration_s:g}"
    init_output = f"{segment_dir.rstrip('/')}/{init_name}"
    segment_pattern = f"{segment_dir.rstrip('/')}/{segment_template}"
    media_playlist = f"{segment_dir.rstrip('/')}/{media_playlist_name}"
    encode = build_encode_plan(source=source, rendition=rendition, spec=spec, output="pipe:")
    # Reuse the encode's filter/codec args but swap the output muxer.
    enc_args = encode.args[:-1]  # drop the "pipe:"/output placeholder
    # Remove faststart (HLS fmp4 doesn't want a relocated moov) for clarity.
    enc_args = _drop_flag_pair(enc_args, "-movflags")
    args = [
        *enc_args,
        "-f",
        "hls",
        "-hls_time",
        seg_time,
        "-hls_playlist_type",
        "vod",
        "-hls_segment_type",
        "fmp4",
        "-hls_flags",
        "independent_segments",
        "-hls_fmp4_init_filename",
        init_name,
        "-hls_segment_filename",
        segment_pattern,
        media_playlist,
    ]
    return SegmentationPlan(
        args=args,
        init_output=init_output,
        segment_pattern=segment_pattern,
        media_playlist=media_playlist,
        expected_segment_count=len(segment_durations),
        segment_durations=[round(d, 3) for d in segment_durations],
    )


def build_dash_segmenting_plan(
    *,
    sources_by_rendition: Sequence[tuple[Rendition, str]],
    spec: NormalizationSpec,
    segment_durations: Sequence[float],
    out_mpd: str,
) -> SegmentationPlan:
    """Build a single ffmpeg ``-f dash`` invocation packaging all renditions.

    DASH's ffmpeg muxer takes every rendition as an input + an ``-map`` per
    output stream and writes one ``.mpd`` plus per-rendition init + ``.m4s``
    fragments. ``-use_template 1 -use_timeline 1`` yields SegmentTemplate-based
    addressing (the compact, growable DASH form the live manifest appends to).
    """
    if not sources_by_rendition:
        raise SegmentationError("at least one rendition source is required for DASH")
    if not segment_durations:
        raise SegmentationError("segment_durations must be non-empty")
    seg_time = f"{spec.segment_duration_s:g}"
    args: list[str] = ["ffmpeg", "-hide_banner", "-y"]
    for _, src in sources_by_rendition:
        args += ["-i", src]
    # Map every input's first video+audio into adaptation sets.
    for idx, _ in enumerate(sources_by_rendition):
        args += ["-map", f"{idx}:v:0", "-map", f"{idx}:a:0?"]
    args += [
        "-c",
        "copy",  # inputs are already normalized renditions
        "-f",
        "dash",
        "-seg_duration",
        seg_time,
        "-use_template",
        "1",
        "-use_timeline",
        "1",
        "-init_seg_name",
        "init-$RepresentationID$.m4s",
        "-media_seg_name",
        "chunk-$RepresentationID$-$Number%05d$.m4s",
        out_mpd,
    ]
    return SegmentationPlan(
        args=args,
        init_output="init-$RepresentationID$.m4s",
        segment_pattern="chunk-$RepresentationID$-$Number%05d$.m4s",
        media_playlist=out_mpd,
        expected_segment_count=len(segment_durations),
        segment_durations=[round(d, 3) for d in segment_durations],
    )


def _drop_flag_pair(args: list[str], flag: str) -> list[str]:
    """Return ``args`` with ``flag`` and its following value removed (if present)."""
    out: list[str] = []
    skip = False
    for token in args:
        if skip:
            skip = False
            continue
        if token == flag:
            skip = True
            continue
        out.append(token)
    return out


def plan_byte_ranges(
    sizes: Sequence[int], *, init_size: int = 0
) -> tuple[ByteRange | None, list[ByteRange]]:
    """Compute init + segment byte ranges for a single-file fMP4 layout.

    When init + all fragments share one ``.mp4`` (single-file addressing), each
    resource is a contiguous byte range. The init occupies ``[0, init_size)``;
    each subsequent segment starts where the previous ended. Returns
    ``(init_range_or_None, [segment_ranges])``.

    Raises:
        SegmentationError: on a negative size.
    """
    if init_size < 0 or any(s < 0 for s in sizes):
        raise SegmentationError("byte-range sizes must be non-negative")
    offset = 0
    init_range: ByteRange | None = None
    if init_size > 0:
        init_range = ByteRange(offset=0, length=init_size)
        offset = init_size
    ranges: list[ByteRange] = []
    for size in sizes:
        if size == 0:
            raise SegmentationError("a media segment cannot be zero bytes")
        ranges.append(ByteRange(offset=offset, length=size))
        offset += size
    return init_range, ranges
