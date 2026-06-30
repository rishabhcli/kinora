"""The canonical normalization *target* — one declarative description of what a
stitch-ready Kinora clip must be, decoupled from any ffmpeg detail.

A provider's clip is "normalised" when it matches a :class:`NormalizationTarget`
on every axis the downstream stitch cares about: geometry + aspect-fit strategy,
fps, video codec / pixel-format, colour tags + range, and (optionally) loudness.
The plan layer (:mod:`app.video.normalize.plan`) turns this declarative target
into the exact ffmpeg arg list; nothing else needs to know the encoder flags.

Built either explicitly or via :meth:`NormalizationTarget.from_settings`, which
reads the ``normalize_*`` block of :class:`app.core.config.Settings` so the whole
pipeline re-targets from one place.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AspectStrategy(StrEnum):
    """How to reconcile a source aspect ratio with the target geometry."""

    #: Letterbox / pillarbox — scale to fit *inside* the frame, pad the rest. No
    #: content is ever lost; the default for mixed-provider sources.
    PAD = "pad"
    #: Scale to *fill* the frame then centre-crop the overflow (a focal-point hint
    #: can bias which edge is kept). Fills edge-to-edge but may crop content.
    CROP = "crop"
    #: Force the exact target dimensions, ignoring aspect — distorts non-matching
    #: sources. Discouraged; offered for completeness / deliberate use.
    STRETCH = "stretch"
    #: Scale preserving aspect with no pad/crop — the output may not equal the
    #: target dimensions exactly (used when a caller only wants a codec/fps fix).
    NONE = "none"


class ColorTags(BaseModel):
    """The colour metadata every canonical clip is tagged with.

    Mixed-provider clips disagree on primaries/transfer/space/range, which makes
    a stitched film flicker in colour. Re-tagging (and converting the range) to a
    single profile is what makes them interchangeable. ``None`` on any field
    leaves that tag untouched.
    """

    model_config = ConfigDict(extra="forbid")

    primaries: str | None = "bt709"
    transfer: str | None = "bt709"
    space: str | None = "bt709"
    range: str | None = "tv"  # "tv" (limited) | "pc" (full)

    @property
    def is_noop(self) -> bool:
        return not any((self.primaries, self.transfer, self.space, self.range))


class LoudnessTarget(BaseModel):
    """An optional EBU R128 loudness-normalisation target for the audio track.

    ``integrated_lufs == 0`` means *disabled* — the encode keeps the source
    loudness untouched (the common case; a single ``loudnorm`` pass is only worth
    it when a clip's narration was mastered hotter/quieter than the rest).
    """

    model_config = ConfigDict(extra="forbid")

    #: Target integrated loudness in LUFS. 0.0 disables the pass entirely.
    integrated_lufs: float = 0.0
    #: Max true peak in dBTP.
    true_peak: float = -1.5
    #: Target loudness range (LU).
    loudness_range: float = 11.0

    @property
    def enabled(self) -> bool:
        return self.integrated_lufs != 0.0


class FocalPoint(BaseModel):
    """A normalised (0..1) point to keep in frame when an aspect strategy crops.

    ``(0.5, 0.5)`` is the centre (the default). A character's face at the top
    third would be ``(0.5, 0.33)``; the crop window is biased to keep that point
    visible instead of blindly centre-cropping the subject out of frame.
    """

    model_config = ConfigDict(extra="forbid")

    x: float = Field(default=0.5, ge=0.0, le=1.0)
    y: float = Field(default=0.5, ge=0.0, le=1.0)

    @classmethod
    def center(cls) -> FocalPoint:
        return cls(x=0.5, y=0.5)


class NormalizationTarget(BaseModel):
    """The full declarative description of a stitch-ready canonical clip."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=720, gt=0)
    height: int = Field(default=1280, gt=0)
    fps: int = Field(default=30, gt=0)
    aspect: AspectStrategy = AspectStrategy.PAD
    focal_point: FocalPoint = Field(default_factory=FocalPoint.center)
    #: Hex/ffmpeg colour name used to pad the letter/pillarbox bars.
    pad_color: str = "black"

    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"
    x264_preset: str = "veryfast"
    crf: int = Field(default=20, ge=0, le=51)

    audio_codec: str = "aac"
    audio_bitrate: str = "128k"
    audio_sample_rate: int = Field(default=48000, gt=0)
    audio_channels: int = Field(default=2, gt=0)

    color: ColorTags = Field(default_factory=ColorTags)
    loudness: LoudnessTarget = Field(default_factory=LoudnessTarget)

    @property
    def dimensions(self) -> tuple[int, int]:
        return (self.width, self.height)

    @model_validator(mode="after")
    def _even_dimensions(self) -> NormalizationTarget:
        """yuv420p chroma subsampling requires even dimensions; reject odd ones."""
        if self.pixel_format == "yuv420p" and (self.width % 2 or self.height % 2):
            raise ValueError(
                f"yuv420p requires even dimensions, got {self.width}x{self.height}"
            )
        return self

    @classmethod
    def from_settings(cls, settings: Any) -> NormalizationTarget:
        """Build the canonical target from the ``normalize_*`` settings block."""
        return cls(
            width=int(settings.normalize_target_width),
            height=int(settings.normalize_target_height),
            fps=int(settings.normalize_target_fps),
            aspect=AspectStrategy(str(settings.normalize_aspect_strategy).lower()),
            video_codec=str(settings.normalize_video_codec),
            pixel_format=str(settings.normalize_pixel_format),
            x264_preset=str(settings.normalize_x264_preset),
            crf=int(settings.normalize_x264_crf),
            audio_codec=str(settings.normalize_audio_codec),
            audio_bitrate=str(settings.normalize_audio_bitrate),
            audio_sample_rate=int(settings.normalize_audio_sample_rate),
            audio_channels=int(settings.normalize_audio_channels),
            color=ColorTags(
                primaries=str(settings.normalize_color_primaries),
                transfer=str(settings.normalize_color_transfer),
                space=str(settings.normalize_color_space),
                range=str(settings.normalize_color_range),
            ),
            loudness=LoudnessTarget(
                integrated_lufs=float(settings.normalize_target_lufs),
                true_peak=float(settings.normalize_loudness_true_peak),
                loudness_range=float(settings.normalize_loudness_range),
            ),
        )


__all__ = [
    "AspectStrategy",
    "ColorTags",
    "FocalPoint",
    "LoudnessTarget",
    "NormalizationTarget",
]
