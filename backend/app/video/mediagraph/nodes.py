"""Declarative media-transform nodes — each owns its ffmpeg arg-plan.

Every node is a pure, frozen description of one media derivation:

* it declares the logical **inputs** it consumes (by name) and the **outputs** it
  produces (:class:`ArtifactRef`);
* it carries a deterministic **signature** (its kind + every knob that changes the
  bytes it emits) that feeds the content-hash cache key;
* it builds an ordered **arg-plan** — the exact ffmpeg/ffprobe invocations — from
  a resolved :class:`PlanContext` (input paths + an output directory).

No node touches the filesystem, a subprocess, a provider, or the network: the
arg-plan is pure data, so the whole planning layer is unit-testable without
ffmpeg. The executor (:mod:`app.video.mediagraph.engine`) runs the plan over an
injectable runner.

The node catalogue covers the full derived-media set a finished clip needs
regardless of which model produced it (§4 delivery): probe, normalize,
extract-last-frame, thumbnail, poster, preview-gif, scrubbing-sprite-sheet,
caption-burn-in, loudness-normalize, watermark.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.video.mediagraph.types import (
    ArtifactRef,
    FfmpegInvocation,
    Geometry,
    MediaKind,
    NodeKind,
)

# --------------------------------------------------------------------------- #
# Plan context — the resolved environment a node plans against
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlanContext:
    """Everything a node needs to emit its concrete arg-plan.

    Pure data: the engine builds one per node, mapping each declared input name to
    a resolved path and giving an output directory. Nodes read paths from here and
    never invent their own — so the plan is fully determined by the graph wiring.
    """

    #: Logical input name → resolved on-disk path of the produced upstream artifact.
    inputs: Mapping[str, Path]
    #: The directory this node writes its outputs into.
    out_dir: Path

    def input_path(self, name: str) -> Path:
        try:
            return self.inputs[name]
        except KeyError as exc:  # pragma: no cover - guarded by graph validation
            raise KeyError(f"plan context missing required input {name!r}") from exc

    def out_path(self, ref: ArtifactRef) -> Path:
        return self.out_dir / ref.filename


# --------------------------------------------------------------------------- #
# Base node
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class TransformNode(ABC):
    """A single declarative media transform.

    Subclasses set :attr:`kind`, declare their input names + output refs, and
    implement :meth:`build_invocations`. The base supplies the cache signature and
    standard validation. Frozen + hashable so a node is itself a value.
    """

    #: Stable graph-unique id (the node's address; also its log label).
    node_id: str

    kind: NodeKind = field(init=False, default=NodeKind.SOURCE)

    # -- declared topology -------------------------------------------------- #

    @property
    @abstractmethod
    def input_names(self) -> tuple[str, ...]:
        """Logical names of the upstream artifacts this node consumes."""

    @property
    @abstractmethod
    def outputs(self) -> tuple[ArtifactRef, ...]:
        """The artifacts this node produces."""

    def output(self, name: str) -> ArtifactRef:
        """The single output ref named ``name`` (errors if absent/ambiguous)."""
        matches = [o for o in self.outputs if o.name == name]
        if len(matches) != 1:
            raise KeyError(f"{self.node_id}: no unique output named {name!r}")
        return matches[0]

    @property
    def primary_output(self) -> ArtifactRef:
        """The first declared output (the node's canonical product)."""
        outs = self.outputs
        if not outs:
            raise ValueError(f"{self.node_id}: node declares no outputs")
        return outs[0]

    # -- cache signature ---------------------------------------------------- #

    def signature(self) -> tuple[Any, ...]:
        """A deterministic descriptor of *what bytes this node emits*.

        Feeds the content-hash cache key. Two nodes with the same signature and
        the same upstream content produce identical bytes, so the second run is a
        cache hit. Subclasses extend via :meth:`signature_extra`.
        """
        return (
            self.kind.value,
            self.input_names,
            tuple((o.name, o.kind.value, o.ext) for o in self.outputs),
            *self.signature_extra(),
        )

    def signature_extra(self) -> tuple[Any, ...]:
        """Subclass knobs that change the produced bytes (override to extend)."""
        return ()

    # -- arg-plan ----------------------------------------------------------- #

    @abstractmethod
    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        """The ordered ffmpeg/ffprobe invocations producing this node's outputs."""

    # Hashing on node_id keeps nodes usable as dict keys / set members.
    def __hash__(self) -> int:  # pragma: no cover - trivial
        return hash(self.node_id)


# --------------------------------------------------------------------------- #
# Shared ffmpeg fragments
# --------------------------------------------------------------------------- #

#: x264 encode tail shared by every video-producing node — yuv420p for universal
#: playback, faststart so the moov atom is front-loaded for web streaming.
_X264_TAIL: tuple[str, ...] = (
    "-c:v",
    "libx264",
    "-preset",
    "veryfast",
    "-pix_fmt",
    "yuv420p",
    "-movflags",
    "+faststart",
)


def _even(value: int) -> int:
    """Round a dimension to the nearest even pixel (x264/yuv420p require even)."""
    return value if value % 2 == 0 else value + 1


# --------------------------------------------------------------------------- #
# SourceNode — the graph root (a no-op that exposes the input clip)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceNode(TransformNode):
    """The graph's source clip — a leaf the derivations hang off of.

    Produces nothing itself (the bytes already exist on disk); it exists so every
    derivation can declare a single, named upstream and the topology is explicit.
    """

    kind: NodeKind = field(init=False, default=NodeKind.SOURCE)
    media: MediaKind = MediaKind.VIDEO
    ext: str = "mp4"
    output_name: str = "source"

    @property
    def input_names(self) -> tuple[str, ...]:
        return ()

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.output_name, kind=self.media, ext=self.ext),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (self.media.value, self.ext)

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        # The source is supplied, not produced — nothing to run.
        return ()


# --------------------------------------------------------------------------- #
# ProbeNode — read facts off a clip (ffprobe; pure metadata, no file output)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProbeNode(TransformNode):
    """Probe a media file with ffprobe — captures stream/format JSON to stdout.

    Side-effect-free: it produces a :class:`MediaKind.SIDECAR` artifact whose
    bytes are the probe JSON the executor writes out, so downstream nodes (and
    callers) can read duration / geometry / codecs without re-probing.
    """

    kind: NodeKind = field(init=False, default=NodeKind.PROBE)
    source: str = "source"
    out_name: str = "probe"

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.SIDECAR, ext="json"),)

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        return (
            FfmpegInvocation(
                binary="ffprobe",
                args=(
                    "-v",
                    "error",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(src),
                ),
                produces=self.outputs[0],
                captures_stdout=True,
                label=f"{self.node_id}:probe",
            ),
        )


# --------------------------------------------------------------------------- #
# NormalizeNode — the canonical master (re-encode to a uniform spec)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class NormalizeNode(TransformNode):
    """Re-encode a clip to Kinora's canonical master spec (uniform geometry/fps).

    Provider clips arrive at assorted resolutions, frame-rates, and codecs; the
    normalised master is the single source every other derivative reads, so the
    aspect/fps never jumps mid-event. Letterboxes (``pad``) rather than crops by
    default so no content is lost; ``crop=True`` fills the frame instead.
    """

    kind: NodeKind = field(init=False, default=NodeKind.NORMALIZE)
    source: str = "source"
    out_name: str = "master"
    geometry: Geometry = Geometry(width=720, height=1280)
    fps: int = 30
    crf: int = 20
    crop: bool = False
    audio_bitrate: str = "128k"

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.VIDEO, ext="mp4"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (
            self.geometry.width,
            self.geometry.height,
            self.fps,
            self.crf,
            self.crop,
            self.audio_bitrate,
        )

    def _scale_filter(self) -> str:
        w, h = self.geometry.width, self.geometry.height
        if self.crop:
            # Fill the frame, centre-crop the overflow.
            return f"scale={w}:{h}:force_original_aspect_ratio=increase," f"crop={w}:{h},setsar=1"
        # Fit inside the frame, pad the remainder (letterbox/pillarbox).
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        return (
            FfmpegInvocation(
                args=(
                    "-y",
                    "-i",
                    str(src),
                    "-vf",
                    f"{self._scale_filter()},fps={self.fps},format=yuv420p",
                    "-crf",
                    str(self.crf),
                    *_X264_TAIL,
                    "-c:a",
                    "aac",
                    "-b:a",
                    self.audio_bitrate,
                    str(out_path),
                ),
                produces=out,
                label=f"{self.node_id}:normalize",
            ),
        )


# --------------------------------------------------------------------------- #
# ExtractLastFrameNode — the continuation anchor still (§9.6)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ExtractLastFrameNode(TransformNode):
    """Extract a single frame (default: the *last*) as a PNG still.

    The last accepted frame is the §9.6 continuation anchor a follow-on shot
    extends; it is also a handy poster source. ``at`` may be ``"last"`` (default),
    ``"first"``, or a float second offset.
    """

    kind: NodeKind = field(init=False, default=NodeKind.EXTRACT_FRAME)
    source: str = "source"
    out_name: str = "last_frame"
    at: str | float = "last"

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.IMAGE, ext="png"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (str(self.at),)

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        args: tuple[str, ...]
        if self.at == "last":
            # Seek to the very end: grab the final decoded frame via reverse+1.
            args = (
                "-y",
                "-sseof",
                "-0.1",
                "-i",
                str(src),
                "-vsync",
                "0",
                "-update",
                "1",
                "-frames:v",
                "1",
                str(out_path),
            )
        elif self.at == "first":
            args = ("-y", "-i", str(src), "-frames:v", "1", "-update", "1", str(out_path))
        else:
            ts = float(self.at)
            args = (
                "-y",
                "-ss",
                f"{ts:.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-update",
                "1",
                str(out_path),
            )
        return (FfmpegInvocation(args=args, produces=out, label=f"{self.node_id}:frame@{self.at}"),)


# --------------------------------------------------------------------------- #
# ThumbnailNode — a small representative still
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ThumbnailNode(TransformNode):
    """A small representative still grabbed at a fractional offset into the clip.

    Defaults to 50 % in (a mid-clip frame is more representative than frame 0,
    which is often black/fade-in). Scaled to fit ``geometry``; jpg for size.
    """

    kind: NodeKind = field(init=False, default=NodeKind.THUMBNAIL)
    source: str = "source"
    out_name: str = "thumb"
    geometry: Geometry = Geometry(width=360, height=640)
    at_fraction: float = 0.5
    quality: int = 4  # ffmpeg mjpeg q:v (2=best .. 31=worst)

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.IMAGE, ext="jpg"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (self.geometry.width, self.geometry.height, round(self.at_fraction, 4), self.quality)

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        w, h = self.geometry.width, self.geometry.height
        # ``thumbnail`` picks the most representative frame in a window; combine
        # with a fractional seek so we land near ``at_fraction`` of the clip.
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2"
        )
        return (
            FfmpegInvocation(
                args=(
                    "-y",
                    "-i",
                    str(src),
                    "-vf",
                    f"thumbnail,{vf}",
                    "-frames:v",
                    "1",
                    "-q:v",
                    str(self.quality),
                    "-update",
                    "1",
                    str(out_path),
                ),
                produces=out,
                label=f"{self.node_id}:thumbnail",
            ),
        )


# --------------------------------------------------------------------------- #
# PosterNode — a full-frame still (the library card art)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PosterNode(TransformNode):
    """A full-resolution poster still at the master geometry (library card art).

    Unlike the thumbnail this keeps full geometry and emits a lossless-ish PNG, so
    it doubles as a share/OG image source. Grabbed at ``at_fraction`` of the clip.
    """

    kind: NodeKind = field(init=False, default=NodeKind.POSTER)
    source: str = "source"
    out_name: str = "poster"
    geometry: Geometry = Geometry(width=720, height=1280)
    at_fraction: float = 0.25

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.IMAGE, ext="png"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (self.geometry.width, self.geometry.height, round(self.at_fraction, 4))

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        w, h = self.geometry.width, self.geometry.height
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )
        return (
            FfmpegInvocation(
                args=(
                    "-y",
                    "-i",
                    str(src),
                    "-vf",
                    f"thumbnail,{vf}",
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    str(out_path),
                ),
                produces=out,
                label=f"{self.node_id}:poster",
            ),
        )


# --------------------------------------------------------------------------- #
# PreviewGifNode — a short looping animated preview
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PreviewGifNode(TransformNode):
    """A short, palette-optimised animated GIF preview (hover-to-play art).

    Two-pass palette generation (palettegen → paletteuse) keeps the GIF small and
    clean. Samples a ``duration_s`` window starting at ``start_s`` at a low
    ``fps``. The two passes share one intermediate palette PNG kept in the output
    dir, so the plan is a fixed two-invocation sequence.
    """

    kind: NodeKind = field(init=False, default=NodeKind.PREVIEW_GIF)
    source: str = "source"
    out_name: str = "preview"
    width: int = 320
    fps: int = 12
    start_s: float = 0.0
    duration_s: float = 3.0

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.GIF, ext="gif"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (self.width, self.fps, round(self.start_s, 3), round(self.duration_s, 3))

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        palette = ctx.out_dir / f"{self.node_id}.palette.png"
        w = _even(self.width)
        scale_fps = f"fps={self.fps},scale={w}:-2:flags=lanczos"
        gen = FfmpegInvocation(
            args=(
                "-y",
                "-ss",
                f"{self.start_s:.3f}",
                "-t",
                f"{self.duration_s:.3f}",
                "-i",
                str(src),
                "-vf",
                f"{scale_fps},palettegen=stats_mode=diff",
                str(palette),
            ),
            label=f"{self.node_id}:palettegen",
        )
        use = FfmpegInvocation(
            args=(
                "-y",
                "-ss",
                f"{self.start_s:.3f}",
                "-t",
                f"{self.duration_s:.3f}",
                "-i",
                str(src),
                "-i",
                str(palette),
                "-lavfi",
                f"{scale_fps}[x];[x][1:v]paletteuse=dither=bayer",
                str(out_path),
            ),
            produces=out,
            label=f"{self.node_id}:paletteuse",
        )
        return (gen, use)


# --------------------------------------------------------------------------- #
# ScrubbingSpriteSheetNode — a tiled timeline of thumbnails (seek-bar preview)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ScrubbingSpriteSheetNode(TransformNode):
    """A scrubbing sprite-sheet: a tiled grid of timeline thumbnails.

    The reading-room seek bar shows a frame preview as the reader scrubs; rather
    than seek the video repeatedly, the client samples one mosaic image. This node
    extracts ``columns × rows`` evenly-spaced frames and tiles them into a single
    sheet. A JSON sidecar records the tile geometry so the client can map a scrub
    position to a sprite cell.
    """

    kind: NodeKind = field(init=False, default=NodeKind.SPRITE_SHEET)
    source: str = "source"
    out_name: str = "sprite"
    sidecar_name: str = "sprite_manifest"
    tile_width: int = 160
    tile_height: int = 90
    columns: int = 5
    rows: int = 5
    fps: float = 1.0  # one sampled frame per second of source

    @property
    def tile_count(self) -> int:
        return self.columns * self.rows

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (
            ArtifactRef(name=self.out_name, kind=MediaKind.IMAGE, ext="png"),
            ArtifactRef(name=self.sidecar_name, kind=MediaKind.SIDECAR, ext="json"),
        )

    def signature_extra(self) -> tuple[Any, ...]:
        return (
            self.tile_width,
            self.tile_height,
            self.columns,
            self.rows,
            round(self.fps, 4),
        )

    def manifest(self) -> dict[str, Any]:
        """The tile-geometry sidecar payload (pure data; the engine writes it)."""
        return {
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "columns": self.columns,
            "rows": self.rows,
            "tile_count": self.tile_count,
            "fps": self.fps,
            "sheet": self.output(self.out_name).filename,
        }

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        sheet = self.output(self.out_name)
        sheet_path = ctx.out_path(sheet)
        tw, th = _even(self.tile_width), _even(self.tile_height)
        # Sample at ``fps``, scale each frame, then tile into a columns×rows grid.
        vf = (
            f"fps={self.fps},scale={tw}:{th}:force_original_aspect_ratio=decrease,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2,"
            f"tile={self.columns}x{self.rows}"
        )
        # Only the SHEET is an ffmpeg output; the manifest sidecar is written by
        # the engine from ``manifest()`` (pure data, no subprocess needed).
        return (
            FfmpegInvocation(
                args=(
                    "-y",
                    "-i",
                    str(src),
                    "-vf",
                    vf,
                    "-frames:v",
                    "1",
                    "-update",
                    "1",
                    str(sheet_path),
                ),
                produces=sheet,
                label=f"{self.node_id}:sprite",
            ),
        )


# --------------------------------------------------------------------------- #
# CaptionBurnInNode — hard-subtitle a captions file onto the video
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CaptionBurnInNode(TransformNode):
    """Burn a captions file (vtt/srt) into the video as hard subtitles.

    Consumes *two* inputs — the video and a captions sidecar — so it is a join
    node (the graph fans these two branches back together). The ``subtitles``
    filter rasterises the captions over each frame; the re-encode tail matches the
    master spec. ``style`` is an optional libass force_style string.
    """

    kind: NodeKind = field(init=False, default=NodeKind.CAPTION_BURN_IN)
    source: str = "master"
    captions: str = "captions"
    out_name: str = "captioned"
    style: str | None = None
    crf: int = 20

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source, self.captions)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.VIDEO, ext="mp4"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (self.style or "", self.crf)

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        subs = ctx.input_path(self.captions)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        # Escape the subtitle path for the filtergraph (':' and '\' are special).
        subs_arg = _escape_subtitles_path(str(subs))
        sub_filter = f"subtitles={subs_arg}"
        if self.style:
            sub_filter += f":force_style='{self.style}'"
        return (
            FfmpegInvocation(
                args=(
                    "-y",
                    "-i",
                    str(src),
                    "-vf",
                    f"{sub_filter},format=yuv420p",
                    "-crf",
                    str(self.crf),
                    *_X264_TAIL,
                    "-c:a",
                    "copy",
                    str(out_path),
                ),
                produces=out,
                label=f"{self.node_id}:burn-in",
            ),
        )


def _escape_subtitles_path(path: str) -> str:
    """Escape a path for the ffmpeg ``subtitles=`` filter option.

    Inside a filtergraph, ``\\`` and ``:`` must be escaped, and the whole value is
    wrapped in single quotes so spaces survive.
    """
    escaped = path.replace("\\", "\\\\").replace(":", "\\:")
    return f"'{escaped}'"


# --------------------------------------------------------------------------- #
# LoudnessNormalizeNode — EBU R128 loudness-normalised audio master
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class LoudnessNormalizeNode(TransformNode):
    """EBU R128 loudness-normalise the audio to a target integrated LUFS.

    Different providers / TTS engines deliver audio at wildly different levels; a
    consistent loudness keeps the reading-room volume stable across clips. Uses
    ffmpeg's ``loudnorm`` (single-pass) targeting ``target_lufs`` / ``true_peak``.
    Produces a re-muxed video (audio re-encoded, video stream copied) — or, when
    ``audio_only``, a standalone normalised audio track.
    """

    kind: NodeKind = field(init=False, default=NodeKind.LOUDNESS)
    source: str = "master"
    out_name: str = "loudnorm"
    target_lufs: float = -16.0
    true_peak: float = -1.5
    lra: float = 11.0
    audio_only: bool = False
    audio_bitrate: str = "192k"

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source,)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        if self.audio_only:
            return (ArtifactRef(name=self.out_name, kind=MediaKind.AUDIO, ext="m4a"),)
        return (ArtifactRef(name=self.out_name, kind=MediaKind.VIDEO, ext="mp4"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (
            round(self.target_lufs, 3),
            round(self.true_peak, 3),
            round(self.lra, 3),
            self.audio_only,
            self.audio_bitrate,
        )

    def _loudnorm_filter(self) -> str:
        return f"loudnorm=I={self.target_lufs}:TP={self.true_peak}:LRA={self.lra}"

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        common = (
            "-y",
            "-i",
            str(src),
            "-af",
            self._loudnorm_filter(),
            "-c:a",
            "aac",
            "-b:a",
            self.audio_bitrate,
        )
        args: tuple[str, ...]
        if self.audio_only:
            args = (*common, "-vn", str(out_path))
        else:
            args = (*common, "-c:v", "copy", "-movflags", "+faststart", str(out_path))
        return (FfmpegInvocation(args=args, produces=out, label=f"{self.node_id}:loudnorm"),)


# --------------------------------------------------------------------------- #
# WatermarkNode — overlay a logo/bug onto the video
# --------------------------------------------------------------------------- #


class WatermarkCorner(StrEnum):
    """Where the watermark sits (overlay ``x:y`` is derived from this)."""

    TOP_LEFT = "top_left"
    TOP_RIGHT = "top_right"
    BOTTOM_LEFT = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


@dataclass(frozen=True)
class WatermarkNode(TransformNode):
    """Overlay a PNG watermark (logo/bug) onto the video at a corner.

    A second input (the watermark image) joins the video branch. The mark is
    scaled to ``mark_width`` px wide, given ``opacity``, and inset ``margin`` px
    from the chosen corner. The video re-encodes; audio is copied.
    """

    kind: NodeKind = field(init=False, default=NodeKind.WATERMARK)
    source: str = "master"
    mark: str = "watermark_img"
    out_name: str = "watermarked"
    corner: WatermarkCorner = WatermarkCorner.BOTTOM_RIGHT
    mark_width: int = 120
    margin: int = 24
    opacity: float = 0.85
    crf: int = 20

    @property
    def input_names(self) -> tuple[str, ...]:
        return (self.source, self.mark)

    @property
    def outputs(self) -> tuple[ArtifactRef, ...]:
        return (ArtifactRef(name=self.out_name, kind=MediaKind.VIDEO, ext="mp4"),)

    def signature_extra(self) -> tuple[Any, ...]:
        return (
            self.corner.value,
            self.mark_width,
            self.margin,
            round(self.opacity, 3),
            self.crf,
        )

    def _overlay_xy(self) -> str:
        m = self.margin
        # ``W``/``H`` = main video dims, ``w``/``h`` = (scaled) overlay dims.
        return {
            WatermarkCorner.TOP_LEFT: f"{m}:{m}",
            WatermarkCorner.TOP_RIGHT: f"W-w-{m}:{m}",
            WatermarkCorner.BOTTOM_LEFT: f"{m}:H-h-{m}",
            WatermarkCorner.BOTTOM_RIGHT: f"W-w-{m}:H-h-{m}",
        }[self.corner]

    def build_invocations(self, ctx: PlanContext) -> tuple[FfmpegInvocation, ...]:
        src = ctx.input_path(self.source)
        mark = ctx.input_path(self.mark)
        out = self.outputs[0]
        out_path = ctx.out_path(out)
        # Scale the mark, apply opacity via colorchannelmixer alpha, then overlay.
        fc = (
            f"[1:v]scale={_even(self.mark_width)}:-1,"
            f"format=rgba,colorchannelmixer=aa={self.opacity}[wm];"
            f"[0:v][wm]overlay={self._overlay_xy()}:format=auto,format=yuv420p[v]"
        )
        return (
            FfmpegInvocation(
                args=(
                    "-y",
                    "-i",
                    str(src),
                    "-i",
                    str(mark),
                    "-filter_complex",
                    fc,
                    "-map",
                    "[v]",
                    "-map",
                    "0:a?",
                    "-crf",
                    str(self.crf),
                    *_X264_TAIL,
                    "-c:a",
                    "copy",
                    str(out_path),
                ),
                produces=out,
                label=f"{self.node_id}:watermark",
            ),
        )


def all_node_types() -> tuple[type[TransformNode], ...]:
    """Every concrete node class (introspection / registry helpers)."""
    return (
        SourceNode,
        ProbeNode,
        NormalizeNode,
        ExtractLastFrameNode,
        ThumbnailNode,
        PosterNode,
        PreviewGifNode,
        ScrubbingSpriteSheetNode,
        CaptionBurnInNode,
        LoudnessNormalizeNode,
        WatermarkNode,
    )


__all__ = [
    "CaptionBurnInNode",
    "ExtractLastFrameNode",
    "LoudnessNormalizeNode",
    "NormalizeNode",
    "PlanContext",
    "PosterNode",
    "PreviewGifNode",
    "ProbeNode",
    "ScrubbingSpriteSheetNode",
    "SourceNode",
    "ThumbnailNode",
    "TransformNode",
    "WatermarkCorner",
    "WatermarkNode",
    "all_node_types",
]
