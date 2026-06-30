"""Domain models for the delivery layer — shots, segments, and stream descriptors.

These pydantic v2 models are the *input* and *intermediate* shapes the packager
and manifest builders pass around. They are deliberately decoupled from the
render-pipeline ``Shot`` ORM (this subsystem is additive and must import-safe
without a DB): a caller adapts a rendered shot into a :class:`ShotClip` at the
seam. Everything here is plain data — no ffmpeg, no I/O.

Vocabulary (so the manifest math reads cleanly):

* **ShotClip** — one finished per-shot mp4 (the render pipeline's unit). Each
  shot becomes one *discontinuity* boundary in the growing film: clips from
  different providers/renditions don't share a continuous timeline, so the HLS
  ``EXT-X-DISCONTINUITY`` / DASH ``Period`` marks the join.
* **MediaSegment** — one fMP4/CMAF segment (a slice of one rendition of one
  shot). A shot of ``D`` seconds segmented at ``T`` seconds yields
  ``ceil(D/T)`` segments, the last possibly short.
* **RenditionTrack** — all the segments of one shot at one rendition.
* **InitSegment** — the per-rendition CMAF init (``moov``/``ftyp``) every
  fragment references; carries the byte-range when init + media share a file.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from app.delivery.errors import ManifestError
from app.delivery.ladder import Rendition


class ByteRange(BaseModel):
    """A byte range into a resource, for byte-range (single-file) addressing.

    Stored as ``offset`` + ``length`` (the HLS ``EXT-X-BYTERANGE`` form is
    ``length@offset``; the DASH ``range`` attribute is ``offset-end``).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    offset: int = Field(ge=0)
    length: int = Field(gt=0)

    @property
    def end(self) -> int:
        """The inclusive last byte index (DASH ``Initialization range`` form)."""
        return self.offset + self.length - 1

    @property
    def hls(self) -> str:
        """The HLS ``EXT-X-BYTERANGE`` value: ``length@offset``."""
        return f"{self.length}@{self.offset}"

    @property
    def http(self) -> str:
        """The HTTP ``Range`` header value: ``bytes=offset-end``."""
        return f"bytes={self.offset}-{self.end}"


class InitSegment(BaseModel):
    """The CMAF initialization segment for one rendition (shared by its fragments)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    uri: str = Field(min_length=1)
    byte_range: ByteRange | None = None


class MediaSegment(BaseModel):
    """One fMP4/CMAF media segment of one rendition of one shot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Monotonic segment number within the whole stream (HLS media sequence).
    sequence: int = Field(ge=0)
    #: 0-based index of this segment *within its shot* (resets per shot).
    index_in_shot: int = Field(ge=0)
    uri: str = Field(min_length=1)
    duration_s: float = Field(gt=0)
    #: True for the first segment of a shot that follows a different shot — the
    #: HLS ``EXT-X-DISCONTINUITY`` / DASH new-Period boundary.
    discontinuity: bool = False
    byte_range: ByteRange | None = None
    #: The shot this segment belongs to (for grouping / discontinuity math).
    shot_id: str = Field(min_length=1)


class RenditionTrack(BaseModel):
    """All segments of one shot at one rendition + the rendition's init segment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rendition: Rendition
    init: InitSegment
    segments: list[MediaSegment] = Field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return round(sum(s.duration_s for s in self.segments), 3)


class ShotClip(BaseModel):
    """A finished per-shot clip ready to be packaged into the growing film.

    The adapter from the render pipeline fills this: ``provider`` drives the
    transcode profile, ``duration_s`` drives segmentation, and ``source_key`` is
    the object-store key of the normalized master mp4.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    shot_id: str = Field(min_length=1)
    #: 0-based position of this shot in the film's playback order.
    order: int = Field(ge=0)
    duration_s: float = Field(gt=0)
    #: Provider/model key (resolved via ``app.delivery.profiles.profile_for``).
    provider: str | None = None
    #: Object-store key (or URL) of the mastered source mp4 for this shot.
    source_key: str = Field(min_length=1)
    #: Source geometry of the master, used to clamp the ladder.
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(default=30, gt=0)

    def segment_count(self, segment_duration_s: float) -> int:
        """How many segments this shot yields at the given segment duration."""
        if segment_duration_s <= 0:
            raise ManifestError("segment_duration_s must be positive")
        return max(1, math.ceil(round(self.duration_s / segment_duration_s, 6)))

    def segment_durations(self, segment_duration_s: float) -> list[float]:
        """The per-segment durations for this shot (last segment may be short).

        The sum equals the shot duration exactly (the remainder is the last
        segment), which the manifest layer asserts so ``EXTINF`` durations and
        the shot total never disagree.
        """
        if segment_duration_s <= 0:
            raise ManifestError("segment_duration_s must be positive")
        count = self.segment_count(segment_duration_s)
        durations = [segment_duration_s] * (count - 1)
        used = segment_duration_s * (count - 1)
        last = round(self.duration_s - used, 3)
        # Guard floating point: a tiny negative/zero remainder collapses the run.
        if last <= 1e-6:
            if durations:
                durations[-1] = round(durations[-1] + max(last, 0.0), 3)
                return durations
            return [round(self.duration_s, 3)]
        durations.append(last)
        return durations


class StreamFormat:
    """The output container/manifest formats this subsystem can package to."""

    HLS = "hls"
    DASH = "dash"


def total_duration(clips: Sequence[ShotClip]) -> float:
    """Sum of shot durations — the full (current) film length."""
    return round(sum(c.duration_s for c in clips), 3)
