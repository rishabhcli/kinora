"""Typed media facts + a *pure* parser from raw ``ffprobe -show_streams`` JSON.

``ffprobe -of json -show_format -show_streams`` emits a deeply nested, stringly
typed blob whose fields differ per container/codec (a Wan clip, a MiniMax clip,
a Ken-Burns mp4, a webm all look different). :func:`parse_ffprobe_json` collapses
that blob into a flat, strongly-typed :class:`MediaInfo` — the single shape the
rest of :mod:`app.video.normalize` reasons over.

The parser is deliberately **pure** (dict in → models out, no subprocess): it is
the part most worth exhaustively unit-testing, and it lets every plan-layer test
synthesise a probe result without touching ffmpeg. :class:`app.video.normalize.
probe.ClipProbe` is the thin ffprobe wrapper that feeds real JSON into it.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: ffprobe reports limited (TV) range as ``tv``/``mpeg`` and full (PC) range as
#: ``pc``/``jpeg``; normalise both spellings to the canonical pair.
_RANGE_ALIASES: dict[str, str] = {
    "tv": "tv",
    "mpeg": "tv",
    "limited": "tv",
    "pc": "pc",
    "jpeg": "pc",
    "full": "pc",
}


def _coerce_float(value: Any) -> float | None:
    """Best-effort float, tolerant of ffprobe's ``"N/A"`` / ``None`` strings."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    # ffprobe occasionally emits NaN/inf for malformed inputs; treat as unknown.
    if f != f or f in (float("inf"), float("-inf")):  # NaN check + infinities
        return None
    return f


def _coerce_int(value: Any) -> int | None:
    f = _coerce_float(value)
    return int(f) if f is not None else None


def parse_rational(value: Any) -> float | None:
    """Parse an ffprobe rational (``"30/1"``, ``"30000/1001"``) or plain number.

    Returns ``None`` for the ``"0/0"`` ffprobe uses when a rate is unknown (so a
    caller never divides by zero), and is robust to already-numeric inputs.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        return f if f > 0 else None
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            frac = Fraction(int(num), int(den)) if int(den) != 0 else None
        except ValueError:
            return None
        return float(frac) if frac and frac > 0 else None
    parsed = _coerce_float(text)
    return parsed if parsed and parsed > 0 else None


class StreamInfo(BaseModel):
    """One decoded media stream (video or audio) as the parser sees it."""

    model_config = ConfigDict(extra="ignore")

    index: int = 0
    codec_type: str  # "video" | "audio" | "subtitle" | ...
    codec_name: str | None = None
    #: Video geometry.
    width: int | None = None
    height: int | None = None
    #: Sample aspect ratio (``"1:1"``) — anamorphic sources have non-square pixels.
    sample_aspect_ratio: str | None = None
    pixel_format: str | None = None
    #: Real frame rate as a float (``r_frame_rate`` preferred, ``avg`` fallback).
    fps: float | None = None
    #: Colour metadata (provider clips frequently disagree on these).
    color_range: str | None = None  # "tv" | "pc"
    color_space: str | None = None
    color_primaries: str | None = None
    color_transfer: str | None = None
    #: Audio facts.
    sample_rate: int | None = None
    channels: int | None = None
    channel_layout: str | None = None
    #: Per-stream duration when present (else the container/format duration leads).
    duration_s: float | None = None
    #: How the rotation tag would re-orient the frame (90/180/270 → swaps W/H).
    rotation: int = 0

    @property
    def is_video(self) -> bool:
        return self.codec_type == "video"

    @property
    def is_audio(self) -> bool:
        return self.codec_type == "audio"

    @property
    def display_dimensions(self) -> tuple[int, int] | None:
        """Width/height after applying a 90°/270° rotation tag (display order)."""
        if self.width is None or self.height is None:
            return None
        if self.rotation in (90, 270):
            return (self.height, self.width)
        return (self.width, self.height)


class MediaInfo(BaseModel):
    """The flat, typed summary of a probed clip the normalizer plans against."""

    model_config = ConfigDict(extra="ignore")

    container: str | None = None  # ffprobe ``format_name`` (e.g. "mov,mp4,...")
    duration_s: float = 0.0
    bit_rate: int | None = None
    size_bytes: int | None = None
    streams: list[StreamInfo] = Field(default_factory=list)

    # -- stream selection ------------------------------------------------- #

    @property
    def video(self) -> StreamInfo | None:
        """The first video stream, or ``None`` (an audio-only / broken clip)."""
        return next((s for s in self.streams if s.is_video), None)

    @property
    def audio(self) -> StreamInfo | None:
        """The first audio stream, or ``None`` (a silent clip)."""
        return next((s for s in self.streams if s.is_audio), None)

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return self.audio is not None

    # -- convenience video facts (display-corrected) ---------------------- #

    @property
    def dimensions(self) -> tuple[int, int] | None:
        """Display W×H of the first video stream (rotation-corrected), or ``None``."""
        return self.video.display_dimensions if self.video else None

    @property
    def fps(self) -> float | None:
        return self.video.fps if self.video else None

    @property
    def aspect_ratio(self) -> float | None:
        """Display aspect ratio (W/H) of the first video stream, or ``None``."""
        dims = self.dimensions
        if not dims or dims[1] == 0:
            return None
        return dims[0] / dims[1]

    def matches_target(
        self,
        *,
        width: int,
        height: int,
        fps: int,
        video_codec: str,
        pixel_format: str,
        color_range: str | None = None,
    ) -> bool:
        """True when this clip already *is* the canonical target on every axis.

        Used to short-circuit a needless transcode: a clip the upstream provider
        happened to emit in the exact target shape can be passed through verbatim
        (still subject to the caller's own copy/re-mux policy).
        """
        v = self.video
        if v is None:
            return False
        if self.dimensions != (width, height):
            return False
        if v.fps is None or abs(v.fps - fps) > 0.01:
            return False
        if (v.codec_name or "") not in _codec_aliases(video_codec):
            return False
        if (v.pixel_format or "") != pixel_format:
            return False
        return not (color_range is not None and (v.color_range or "") != color_range)


def _codec_aliases(target: str) -> frozenset[str]:
    """Map an encoder name (``libx264``) to the codec names ffprobe reports."""
    encoder_to_codec = {
        "libx264": ("h264",),
        "h264": ("h264",),
        "libx265": ("hevc",),
        "hevc": ("hevc",),
        "libvpx-vp9": ("vp9",),
        "vp9": ("vp9",),
    }
    return frozenset(encoder_to_codec.get(target, (target,)))


# --------------------------------------------------------------------------- #
# The pure parser (raw ffprobe JSON dict → MediaInfo)
# --------------------------------------------------------------------------- #


def _stream_rotation(stream: dict[str, Any]) -> int:
    """Extract a rotation tag (degrees) from either side-data or the tag block.

    Modern ffprobe puts rotation in ``side_data_list`` (``rotation: -90``) while
    older mp4s carry a ``tags.rotate`` string. We normalise to a non-negative
    multiple of 90 in ``{0, 90, 180, 270}``.
    """
    raw: Any = None
    for side in stream.get("side_data_list", []) or []:
        if isinstance(side, dict) and "rotation" in side:
            raw = side["rotation"]
            break
    if raw is None:
        raw = (stream.get("tags") or {}).get("rotate")
    deg = _coerce_int(raw)
    if deg is None:
        return 0
    return deg % 360 if deg % 90 == 0 else 0


def _parse_stream(raw: dict[str, Any]) -> StreamInfo:
    fps = parse_rational(raw.get("r_frame_rate")) or parse_rational(raw.get("avg_frame_rate"))
    color_range = raw.get("color_range")
    return StreamInfo(
        index=_coerce_int(raw.get("index")) or 0,
        codec_type=str(raw.get("codec_type") or "unknown"),
        codec_name=raw.get("codec_name"),
        width=_coerce_int(raw.get("width")),
        height=_coerce_int(raw.get("height")),
        sample_aspect_ratio=raw.get("sample_aspect_ratio"),
        pixel_format=raw.get("pix_fmt"),
        fps=fps,
        color_range=_RANGE_ALIASES.get(str(color_range).lower()) if color_range else None,
        color_space=raw.get("color_space"),
        color_primaries=raw.get("color_primaries"),
        color_transfer=raw.get("color_transfer"),
        sample_rate=_coerce_int(raw.get("sample_rate")),
        channels=_coerce_int(raw.get("channels")),
        channel_layout=raw.get("channel_layout"),
        duration_s=_coerce_float(raw.get("duration")),
        rotation=_stream_rotation(raw),
    )


def parse_ffprobe_json(payload: dict[str, Any]) -> MediaInfo:
    """Collapse a raw ``ffprobe -of json`` payload into a typed :class:`MediaInfo`.

    Pure and total: missing / malformed fields degrade to ``None``/``0`` rather
    than raising, so a partially-probed or unusual provider clip still yields a
    usable summary. The format-level ``duration`` leads; when it is absent the
    longest stream duration is used as a fallback so a stream-only probe still
    reports a length.
    """
    fmt = payload.get("format") or {}
    raw_streams = payload.get("streams") or []
    streams = [_parse_stream(s) for s in raw_streams if isinstance(s, dict)]

    duration = _coerce_float(fmt.get("duration"))
    if duration is None:
        stream_durations = [s.duration_s for s in streams if s.duration_s is not None]
        duration = max(stream_durations) if stream_durations else 0.0

    return MediaInfo(
        container=fmt.get("format_name"),
        duration_s=duration,
        bit_rate=_coerce_int(fmt.get("bit_rate")),
        size_bytes=_coerce_int(fmt.get("size")),
        streams=streams,
    )


__all__ = [
    "MediaInfo",
    "StreamInfo",
    "parse_ffprobe_json",
    "parse_rational",
]
