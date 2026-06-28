"""Media inspection — duration, geometry, codecs (a typed reuse of degrade).

The render package already resolves ffmpeg/ffprobe portably and parses a clip
into a :class:`app.render.degrade.ProbeInfo`. This module is a thin, typed bridge
that turns that into the media layer's :class:`app.media.metadata.AssetMetadata`
so the rest of the subsystem speaks one shape — and adds a couple of derivations
the raw probe doesn't (orientation, a "is this the §4.2 vertical film geometry?"
check) that the packaging + sprite code lean on.

No new ffmpeg invocation style is introduced — everything routes through
:func:`app.render.degrade.inspect`, which works on the bundled-``ffmpeg``-only
image (no ``ffprobe`` needed). Callers that have no ffmpeg get a clean
:class:`app.render.degrade.FfmpegError` to skip on.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.media.hashing import sha256_hex
from app.media.metadata import AssetMetadata, MediaAssetKind, guess_content_type

#: The §4.2 canonical vertical film geometry (must match degrade.FILM_SIZE).
FILM_SIZE: tuple[int, int] = (720, 1280)


@dataclass(frozen=True, slots=True)
class MediaProbe:
    """The salient, typed facts about a media blob (superset of ProbeInfo)."""

    duration_s: float
    has_video: bool
    has_audio: bool
    width: int | None
    height: int | None
    video_codec: str | None
    audio_codec: str | None
    nb_streams: int

    @property
    def is_portrait(self) -> bool:
        """True when taller than wide (the reel format)."""
        return bool(self.width and self.height and self.height > self.width)

    @property
    def is_film_geometry(self) -> bool:
        """True when exactly the §4.2 720×1280 vertical film geometry."""
        return (self.width, self.height) == FILM_SIZE

    @property
    def aspect_ratio(self) -> float | None:
        """``width / height`` when both are known."""
        if self.width and self.height:
            return self.width / self.height
        return None


def probe_media(data: bytes) -> MediaProbe:
    """Inspect ``data`` and return a typed :class:`MediaProbe`.

    Raises:
        app.render.degrade.FfmpegError: when no ffmpeg binary is available.
    """
    from app.render.degrade import inspect

    info = inspect(data)
    return MediaProbe(
        duration_s=info.duration_s,
        has_video=info.has_video,
        has_audio=info.has_audio,
        width=info.width,
        height=info.height,
        video_codec=info.video_codec,
        audio_codec=info.audio_codec,
        nb_streams=info.nb_streams,
    )


def metadata_for(
    data: bytes,
    *,
    storage_key: str,
    kind: MediaAssetKind = MediaAssetKind.OTHER,
    content_type: str | None = None,
    book_id: str | None = None,
    probe: bool = True,
) -> AssetMetadata:
    """Build :class:`AssetMetadata` for ``data``, probing AV facts when asked.

    ``probe=True`` (default) runs ffmpeg only for video/audio content; stills and
    documents skip it. If ffmpeg is unavailable the AV fields are simply left
    ``None`` (no raise) so metadata can still be recorded for a clip on a box
    without ffmpeg.
    """
    ctype = content_type or guess_content_type(storage_key)
    width = height = None
    duration_s = None
    if probe and (ctype.startswith("video/") or ctype.startswith("audio/")):
        try:
            mp = probe_media(data)
            width, height = mp.width, mp.height
            duration_s = mp.duration_s or None
        except Exception:  # noqa: BLE001 - ffmpeg absent / unprobeable → leave None
            pass
    return AssetMetadata(
        storage_key=storage_key,
        kind=kind,
        content_type=ctype,
        content_hash=sha256_hex(data),
        size_bytes=len(data),
        width=width,
        height=height,
        duration_s=duration_s,
        book_id=book_id,
    )


__all__ = [
    "FILM_SIZE",
    "MediaProbe",
    "metadata_for",
    "probe_media",
]
