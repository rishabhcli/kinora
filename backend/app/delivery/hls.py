"""HLS manifest builders — master + per-rendition media playlists (RFC 8216).

Pure text builders: given the ladder + the per-shot CMAF segments, these emit
exactly the ``.m3u8`` bytes a player consumes. No ffmpeg, no I/O — so segment
durations, the ``EXT-X-DISCONTINUITY`` at every shot boundary, the
``EXT-X-TARGETDURATION`` ceiling, and the live-window growth are all asserted
deterministically.

Two playlist shapes:

* **Master** (:func:`build_master_playlist`) — one ``EXT-X-STREAM-INF`` per
  rendition pointing at that rendition's media playlist, advertising
  ``BANDWIDTH`` / ``AVERAGE-BANDWIDTH`` / ``RESOLUTION`` / ``CODECS`` /
  ``FRAME-RATE`` so the player can pick a rung up front.
* **Media** (:func:`build_media_playlist`) — the segment list for one rendition.
  A **finished** film is ``EXT-X-PLAYLIST-TYPE:VOD`` + ``EXT-X-ENDLIST``; a
  **growing** film (rendering ahead of the reader) omits ``ENDLIST`` and is a
  live/event playlist that simply appends new ``EXTINF`` lines as shots finish —
  the same window-growth a live stream uses, which is exactly how the reader
  streams *ahead of render*.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.delivery.errors import ManifestError
from app.delivery.ladder import Rendition, validate_ladder
from app.delivery.models import MediaSegment

HLS_VERSION = 7  # CMAF/fMP4 segments require v7+.


def build_master_playlist(
    renditions: Sequence[Rendition],
    *,
    media_playlist_uri: dict[str, str],
) -> str:
    """Build the HLS master playlist for an ABR ladder.

    ``media_playlist_uri`` maps each rendition ``name`` to the (relative or
    signed) URI of its media playlist. Renditions are emitted highest-bandwidth
    first (the convention; also makes output byte-stable).

    Raises:
        ManifestError: on an empty/invalid ladder or a missing media URI.
    """
    validate_ladder(renditions)
    ordered = sorted(renditions, key=lambda r: (-r.peak_bandwidth_bps, r.name))
    lines = ["#EXTM3U", f"#EXT-X-VERSION:{HLS_VERSION}", "#EXT-X-INDEPENDENT-SEGMENTS"]
    for rendition in ordered:
        uri = media_playlist_uri.get(rendition.name)
        if not uri:
            raise ManifestError(f"no media playlist URI for rendition {rendition.name!r}")
        attrs = (
            f"BANDWIDTH={rendition.peak_bandwidth_bps},"
            f"AVERAGE-BANDWIDTH={rendition.average_bandwidth_bps},"
            f'RESOLUTION={rendition.resolution},'
            f'CODECS="{rendition.rfc6381_codecs}",'
            f"FRAME-RATE={rendition.fps:.3f}"
        )
        lines.append(f"#EXT-X-STREAM-INF:{attrs}")
        lines.append(uri)
    return "\n".join(lines) + "\n"


def target_duration(segments: Sequence[MediaSegment]) -> int:
    """The ``EXT-X-TARGETDURATION`` — the ceiling-rounded longest segment.

    RFC 8216 requires every ``EXTINF`` to be <= the integer target duration, so
    this rounds the maximum segment duration *up*.
    """
    if not segments:
        return 0
    return math.ceil(max(s.duration_s for s in segments) - 1e-6)


def build_media_playlist(
    segments: Sequence[MediaSegment],
    *,
    init_uri: str,
    init_byte_range: str | None = None,
    finished: bool,
    media_sequence: int | None = None,
    discontinuity_sequence: int = 0,
    part_target: float | None = None,
) -> str:
    """Build a per-rendition HLS media playlist.

    Args:
        segments: ordered media segments (use :class:`MediaSegment.discontinuity`
            to mark shot boundaries — a ``#EXT-X-DISCONTINUITY`` is emitted before
            each such segment).
        init_uri: the CMAF init segment URI (``#EXT-X-MAP``).
        init_byte_range: optional ``length@offset`` if init is byte-ranged.
        finished: when True the film is complete → VOD + ``#EXT-X-ENDLIST``;
            when False it is a growing/live playlist (no ENDLIST) the reader can
            stream ahead of, with new segments appended as shots finish.
        media_sequence: the ``#EXT-X-MEDIA-SEQUENCE`` (defaults to the first
            segment's ``sequence``) — for a sliding live window this is the
            sequence of the *first present* segment.
        discontinuity_sequence: the ``#EXT-X-DISCONTINUITY-SEQUENCE`` — the count
            of shot boundaries that have already slid out of the window's front,
            so a player resyncs timestamps correctly on a sliding live window.

    Raises:
        ManifestError: on an empty segment list or a non-monotonic sequence.
    """
    if not segments:
        raise ManifestError("media playlist needs at least one segment")
    if discontinuity_sequence < 0:
        raise ManifestError("discontinuity_sequence must be non-negative")
    _assert_monotonic(segments)
    seq0 = media_sequence if media_sequence is not None else segments[0].sequence
    td = target_duration(segments)
    lines = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{HLS_VERSION}",
        f"#EXT-X-TARGETDURATION:{td}",
        f"#EXT-X-MEDIA-SEQUENCE:{seq0}",
        f"#EXT-X-DISCONTINUITY-SEQUENCE:{discontinuity_sequence}",
    ]
    if finished:
        lines.append("#EXT-X-PLAYLIST-TYPE:VOD")
    else:
        lines.append("#EXT-X-PLAYLIST-TYPE:EVENT")
    if part_target is not None:
        lines.append(f"#EXT-X-PART-INF:PART-TARGET={part_target:g}")
    map_line = f'#EXT-X-MAP:URI="{init_uri}"'
    if init_byte_range:
        map_line += f',BYTERANGE="{init_byte_range}"'
    lines.append(map_line)
    for seg in segments:
        if seg.discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXTINF:{seg.duration_s:.3f},")
        if seg.byte_range is not None:
            lines.append(f"#EXT-X-BYTERANGE:{seg.byte_range.hls}")
        lines.append(seg.uri)
    if finished:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _assert_monotonic(segments: Sequence[MediaSegment]) -> None:
    last = -1
    for seg in segments:
        if seg.sequence <= last:
            raise ManifestError(
                f"media-sequence not strictly increasing: {seg.sequence} after {last}"
            )
        last = seg.sequence
