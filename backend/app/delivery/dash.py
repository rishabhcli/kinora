"""MPEG-DASH MPD builder — multi-rendition, shot-boundary Periods, live/static.

A pure XML builder mirroring the HLS one: same ladder, same CMAF segments, the
DASH spelling. No ffmpeg, no I/O.

Structure:

* The **MPD** is ``static`` for a finished film, ``dynamic`` for a growing one.
* Each **shot is its own ``Period``** — this is the DASH equivalent of the HLS
  ``EXT-X-DISCONTINUITY``: clips from different providers/renditions don't share
  one continuous timeline, so a new Period at each shot boundary lets the player
  re-init cleanly (and is how the growing manifest appends — a finished shot
  adds a Period).
* Within a Period, one **AdaptationSet** (video) holds one
  **Representation** per rendition, each pointing at its CMAF init + the
  ``SegmentList`` of that shot's fragments with explicit ``duration``s (so the
  last short segment is exact, not assumed).

We emit an explicit ``SegmentList`` (rather than ``SegmentTemplate``) because
the per-shot segment durations are known and the last one is short — a list is
unambiguous and keeps the manifest math directly assertable.
"""

from __future__ import annotations

from collections.abc import Sequence
from xml.sax.saxutils import quoteattr

from app.delivery.errors import ManifestError
from app.delivery.ladder import Rendition
from app.delivery.models import RenditionTrack

MPD_NS = "urn:mpeg:dash:schema:mpd:2011"
PROFILE_CMAF = "urn:mpeg:dash:profile:isoff-live:2011,urn:mpeg:dash:profile:isoff-on-demand:2011"


def _fmt_duration(seconds: float) -> str:
    """ISO-8601 duration (``PT#H#M#S``) DASH uses for ``mediaPresentationDuration``."""
    seconds = max(0.0, round(seconds, 3))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds - hours * 3600 - minutes * 60
    out = "PT"
    if hours:
        out += f"{hours}H"
    if minutes:
        out += f"{minutes}M"
    # Always emit seconds (with up to ms precision, trimmed) so PT0S never empty.
    out += f"{secs:.3f}".rstrip("0").rstrip(".") + "S" if secs or out == "PT" else ""
    if out == "PT":
        out = "PT0S"
    return out


class ShotPeriod:
    """The per-rendition tracks of one shot — one DASH ``Period``."""

    __slots__ = ("shot_id", "start_s", "duration_s", "tracks")

    def __init__(
        self,
        *,
        shot_id: str,
        start_s: float,
        duration_s: float,
        tracks: Sequence[RenditionTrack],
    ) -> None:
        if not tracks:
            raise ManifestError(f"shot period {shot_id!r} has no rendition tracks")
        self.shot_id = shot_id
        self.start_s = start_s
        self.duration_s = duration_s
        self.tracks = list(tracks)


def build_mpd(
    periods: Sequence[ShotPeriod],
    *,
    dynamic: bool,
    min_buffer_s: float = 4.0,
    publish_time: str | None = None,
    availability_start_time: str | None = None,
) -> str:
    """Build the full MPD for an ordered list of shot Periods.

    Args:
        periods: ordered shot Periods (the growing film's shots in playback order).
        dynamic: True for a still-growing film (``type="dynamic"``, no overall
            ``mediaPresentationDuration``); False for a finished film
            (``type="static"`` with the total duration).
        min_buffer_s: the DASH ``minBufferLength``.
        publish_time / availability_start_time: required by dynamic MPDs (the
            wall-clock anchors); the caller passes ISO timestamps.

    Raises:
        ManifestError: on an empty Period list, or a dynamic MPD missing anchors.
    """
    if not periods:
        raise ManifestError("MPD needs at least one shot period")
    total = round(sum(p.duration_s for p in periods), 3)
    mpd_type = "dynamic" if dynamic else "static"
    attrs = [
        f'xmlns={quoteattr(MPD_NS)}',
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"',
        f'profiles={quoteattr(PROFILE_CMAF)}',
        f'type={quoteattr(mpd_type)}',
        f'minBufferTime={quoteattr(_fmt_duration(min_buffer_s))}',
    ]
    if dynamic:
        if not publish_time or not availability_start_time:
            raise ManifestError("dynamic MPD requires publishTime + availabilityStartTime")
        attrs.append(f"publishTime={quoteattr(publish_time)}")
        attrs.append(f"availabilityStartTime={quoteattr(availability_start_time)}")
    else:
        attrs.append(f"mediaPresentationDuration={quoteattr(_fmt_duration(total))}")

    lines = ['<?xml version="1.0" encoding="UTF-8"?>', f"<MPD {' '.join(attrs)}>"]
    for period in periods:
        lines += _render_period(period)
    lines.append("</MPD>")
    return "\n".join(lines) + "\n"


def _render_period(period: ShotPeriod) -> list[str]:
    pid = quoteattr(f"shot-{period.shot_id}")
    start = quoteattr(_fmt_duration(period.start_s))
    dur = quoteattr(_fmt_duration(period.duration_s))
    lines = [f"  <Period id={pid} start={start} duration={dur}>"]
    # One video AdaptationSet holding every rendition Representation.
    lines.append(
        '    <AdaptationSet contentType="video" mimeType="video/mp4" '
        'segmentAlignment="true" startWithSAP="1">'
    )
    for track in period.tracks:
        lines += _render_representation(track)
    lines.append("    </AdaptationSet>")
    lines.append("  </Period>")
    return lines


def _render_representation(track: RenditionTrack) -> list[str]:
    r: Rendition = track.rendition
    rep_id = quoteattr(r.name)
    codecs = quoteattr(r.rfc6381_codecs.split(",")[0])  # video codec only for video AS
    lines = [
        f'      <Representation id={rep_id} codecs={codecs} '
        f'bandwidth="{r.peak_bandwidth_bps}" width="{r.width}" height="{r.height}" '
        f'frameRate="{r.fps}">'
    ]
    # Timescale in milliseconds keeps the integer durations exact.
    timescale = 1000
    init_attr = quoteattr(track.init.uri)
    if track.init.byte_range is not None:
        lines.append(
            f'        <SegmentList timescale="{timescale}">'
        )
        lines.append(
            f'          <Initialization sourceURL={init_attr} '
            f'range={quoteattr(f"{track.init.byte_range.offset}-{track.init.byte_range.end}")}/>'
        )
    else:
        lines.append(f'        <SegmentList timescale="{timescale}">')
        lines.append(f"          <Initialization sourceURL={init_attr}/>")
    for seg in track.segments:
        d = int(round(seg.duration_s * timescale))
        media = quoteattr(seg.uri)
        if seg.byte_range is not None:
            rng = quoteattr(f"{seg.byte_range.offset}-{seg.byte_range.end}")
            lines.append(f'          <SegmentURL media={media} mediaRange={rng} d="{d}"/>')
        else:
            lines.append(f'          <SegmentURL media={media} d="{d}"/>')
    lines.append("        </SegmentList>")
    lines.append("      </Representation>")
    return lines
