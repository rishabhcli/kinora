"""The growing/live manifest engine — shots → CMAF segments → HLS + DASH.

This is the orchestration the whole subsystem exists for: a book's film grows a
few seconds *ahead of the reader* (kinora.md §5.3 dual-watermark buffer), so the
delivery manifest must be **appendable** — each finished shot adds its segments
to a manifest the reader is already streaming, with no rewrite of the past.

:class:`GrowingFilm` is the in-memory model of the film-so-far: an ordered list
of :class:`~app.delivery.models.ShotClip` s plus the chosen ladder + segment
duration. From it, deterministic builders derive:

* the **flat segment timeline** per rendition — every shot fragmented into CMAF
  segments, each first-segment-of-a-shot marked as a discontinuity (so a player
  re-inits at the provider/rendition boundary), with monotonic media sequence
  numbers across the whole film;
* the **HLS** master + per-rendition media playlists (VOD when ``finished``,
  EVENT/live while growing);
* the **DASH** MPD (static vs dynamic), one Period per shot;
* a **sliding live window** view (:meth:`live_window`) that drops shots that
  have slid out of the back of the window and reports the correct
  media-sequence + discontinuity-sequence so the trimmed playlist is still
  spec-correct.

Everything here is pure (no ffmpeg / I/O): correctness of segment durations,
discontinuity placement, live-window growth/slide, and the emitted manifest text
is fully unit-testable. The URI scheme is injected (:class:`UriScheme`) so the
same engine drives object-store keys, relative paths, or signed URLs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from app.delivery import dash as dash_mod
from app.delivery import hls as hls_mod
from app.delivery.errors import ManifestError
from app.delivery.ladder import Rendition, build_ladder, validate_ladder
from app.delivery.models import (
    InitSegment,
    MediaSegment,
    RenditionTrack,
    ShotClip,
    StreamFormat,
)

#: Default CMAF segment duration. 2.0s @ 30fps = a 60-frame closed GOP that
#: divides evenly (so IDRs land on boundaries — see profiles.normalization_spec)
#: and is small enough for low-latency ahead-of-render streaming.
DEFAULT_SEGMENT_DURATION_S = 2.0


@dataclass(frozen=True)
class UriScheme:
    """Pluggable URI builder for the manifest's child resources.

    Defaults produce stable *relative* paths under a per-rendition directory,
    which a CDN/object-store maps directly. A caller can inject signed-URL or
    object-key variants without the manifest engine knowing about auth/storage.
    """

    #: ``(rendition_name) -> media playlist URI`` (relative to the master).
    media_playlist: Callable[[str], str] = (
        lambda name: f"{name}/media.m3u8"
    )
    #: ``(rendition_name) -> CMAF init segment URI``.
    init_segment: Callable[[str], str] = (
        lambda name: f"{name}/init.mp4"
    )
    #: ``(rendition_name, shot_order, index_in_shot, global_seq) -> segment URI``.
    media_segment: Callable[[str, int, int, int], str] = (
        lambda name, shot_order, idx, seq: f"{name}/seg_{seq:05d}.m4s"
    )


class FilmManifest(BaseModel):
    """The bundle of artifacts a player fetches: master + per-rendition playlists."""

    model_config = ConfigDict(extra="forbid")

    format: str
    finished: bool
    #: HLS only: the master playlist text.
    master: str | None = None
    #: HLS only: ``rendition_name -> media playlist text``.
    media: dict[str, str] = Field(default_factory=dict)
    #: DASH only: the MPD text.
    mpd: str | None = None
    #: Diagnostics: total film duration + segment/shot counts.
    duration_s: float = 0.0
    shot_count: int = 0
    segment_count: int = 0


class GrowingFilm:
    """The film-so-far: ordered shots + ladder + segment duration, appendable.

    Append shots as the render pipeline finishes them; build a manifest at any
    point. The same instance serves the finished VOD and every intermediate
    growing snapshot — the manifest builders just flip ``finished``.
    """

    def __init__(
        self,
        *,
        book_id: str,
        ladder: Sequence[Rendition],
        segment_duration_s: float = DEFAULT_SEGMENT_DURATION_S,
        uri_scheme: UriScheme | None = None,
    ) -> None:
        if segment_duration_s <= 0:
            raise ManifestError("segment_duration_s must be positive")
        validate_ladder(ladder)
        self.book_id = book_id
        self.ladder: list[Rendition] = sorted(
            ladder, key=lambda r: (-r.peak_bandwidth_bps, r.name)
        )
        self.segment_duration_s = segment_duration_s
        self.uri_scheme = uri_scheme or UriScheme()
        self._clips: list[ShotClip] = []

    # -- construction --------------------------------------------------------

    @classmethod
    def from_clips(
        cls,
        *,
        book_id: str,
        clips: Sequence[ShotClip],
        segment_duration_s: float = DEFAULT_SEGMENT_DURATION_S,
        ladder: Sequence[Rendition] | None = None,
        uri_scheme: UriScheme | None = None,
    ) -> GrowingFilm:
        """Build a film from clips, deriving the ladder from the first clip's geometry."""
        if not clips and ladder is None:
            raise ManifestError("cannot derive a ladder without clips or an explicit ladder")
        if ladder is None:
            head = clips[0]
            ladder = build_ladder(
                source_width=head.width, source_height=head.height, fps=head.fps
            )
        film = cls(
            book_id=book_id,
            ladder=ladder,
            segment_duration_s=segment_duration_s,
            uri_scheme=uri_scheme,
        )
        for clip in sorted(clips, key=lambda c: c.order):
            film.append(clip)
        return film

    def append(self, clip: ShotClip) -> None:
        """Append a finished shot. Order must be strictly increasing."""
        if self._clips and clip.order <= self._clips[-1].order:
            raise ManifestError(
                f"shot order must increase: {clip.order} after {self._clips[-1].order}"
            )
        self._clips.append(clip)

    @property
    def clips(self) -> list[ShotClip]:
        return list(self._clips)

    @property
    def duration_s(self) -> float:
        return round(sum(c.duration_s for c in self._clips), 3)

    # -- segment timeline ----------------------------------------------------

    def rendition_track(self, rendition: Rendition) -> RenditionTrack:
        """Build the full segment track for one rendition across every shot.

        Each shot is fragmented at ``segment_duration_s``; the first segment of
        every shot after the first is a discontinuity (provider/rendition reset).
        Media sequence numbers are monotonic across the whole film.
        """
        if rendition not in self.ladder:
            raise ManifestError(f"rendition {rendition.name!r} is not in this film's ladder")
        init = InitSegment(uri=self.uri_scheme.init_segment(rendition.name))
        segments: list[MediaSegment] = []
        seq = 0
        for shot_index, clip in enumerate(self._clips):
            durations = clip.segment_durations(self.segment_duration_s)
            for idx, dur in enumerate(durations):
                uri = self.uri_scheme.media_segment(rendition.name, clip.order, idx, seq)
                segments.append(
                    MediaSegment(
                        sequence=seq,
                        index_in_shot=idx,
                        uri=uri,
                        duration_s=dur,
                        discontinuity=(shot_index > 0 and idx == 0),
                        shot_id=clip.shot_id,
                    )
                )
                seq += 1
        return RenditionTrack(rendition=rendition, init=init, segments=segments)

    def all_tracks(self) -> dict[str, RenditionTrack]:
        """Every rendition's full track, keyed by rendition name."""
        return {r.name: self.rendition_track(r) for r in self.ladder}

    # -- live window ---------------------------------------------------------

    def live_window(
        self, *, max_shots: int | None = None, max_duration_s: float | None = None
    ) -> LiveWindow:
        """A sliding view keeping only the most recent shots within the window.

        A live/event stream may retain only the trailing window (older shots
        slide out of the playlist front). This computes which shots remain, the
        ``media_sequence`` of the first retained segment, and the
        ``discontinuity_sequence`` (count of shot boundaries that slid out) so
        the trimmed HLS playlist stays spec-correct.

        With neither bound the window is the whole film (no slide).
        """
        clips = self._clips
        if not clips:
            raise ManifestError("cannot build a live window over an empty film")
        kept_from = 0
        if max_shots is not None and max_shots > 0:
            kept_from = max(kept_from, len(clips) - max_shots)
        if max_duration_s is not None and max_duration_s > 0:
            # Drop from the front until the retained duration fits the window.
            total = self.duration_s
            running = 0.0
            cutoff = 0
            for i, clip in enumerate(clips):
                if total - running <= max_duration_s:
                    cutoff = i
                    break
                running += clip.duration_s
                cutoff = i + 1
            kept_from = max(kept_from, cutoff)
        kept_from = min(kept_from, len(clips) - 1)  # always keep >=1 shot
        # media sequence of the first kept segment = total segments before it.
        seq_before = sum(c.segment_count(self.segment_duration_s) for c in clips[:kept_from])
        # discontinuity sequence = number of shot boundaries before the window.
        # A boundary precedes shot i (i>=1); kept_from boundaries have slid out.
        disc_before = kept_from
        return LiveWindow(
            film=self,
            kept_from=kept_from,
            media_sequence=seq_before,
            discontinuity_sequence=disc_before,
        )

    # -- HLS -----------------------------------------------------------------

    def build_hls(
        self, *, finished: bool, signer: Callable[[str], str] | None = None
    ) -> FilmManifest:
        """Build the HLS master + per-rendition media playlists.

        Args:
            finished: VOD (``finished``) vs growing/EVENT playlists.
            signer: optional ``(uri) -> signed_uri`` applied to the master's
                media-playlist references (segment/init signing is the URI
                scheme's job, applied when the player fetches each media list).
        """
        tracks = self.all_tracks()
        media_uri = {}
        for r in self.ladder:
            uri = self.uri_scheme.media_playlist(r.name)
            media_uri[r.name] = signer(uri) if signer else uri
        master = hls_mod.build_master_playlist(self.ladder, media_playlist_uri=media_uri)
        media: dict[str, str] = {}
        seg_count = 0
        for name, track in tracks.items():
            media[name] = hls_mod.build_media_playlist(
                track.segments,
                init_uri=track.init.uri,
                init_byte_range=track.init.byte_range.hls if track.init.byte_range else None,
                finished=finished,
            )
            seg_count = len(track.segments)  # same across renditions
        return FilmManifest(
            format=StreamFormat.HLS,
            finished=finished,
            master=master,
            media=media,
            duration_s=self.duration_s,
            shot_count=len(self._clips),
            segment_count=seg_count,
        )

    # -- DASH ----------------------------------------------------------------

    def build_dash(
        self,
        *,
        finished: bool,
        publish_time: str | None = None,
        availability_start_time: str | None = None,
    ) -> FilmManifest:
        """Build the DASH MPD (static when ``finished``, dynamic while growing)."""
        if not self._clips:
            raise ManifestError("cannot build a DASH MPD over an empty film")
        # Build per-shot Periods: each Period holds every rendition's track for
        # just that shot, with segment sequences local to the period.
        periods: list[dash_mod.ShotPeriod] = []
        start = 0.0
        # Pre-build the flat tracks so the per-shot slicing reuses URIs/seqs.
        flat = self.all_tracks()
        for clip in self._clips:
            shot_tracks: list[RenditionTrack] = []
            for r in self.ladder:
                full = flat[r.name]
                shot_segs = [s for s in full.segments if s.shot_id == clip.shot_id]
                shot_tracks.append(
                    RenditionTrack(rendition=r, init=full.init, segments=shot_segs)
                )
            periods.append(
                dash_mod.ShotPeriod(
                    shot_id=clip.shot_id,
                    start_s=round(start, 3),
                    duration_s=clip.duration_s,
                    tracks=shot_tracks,
                )
            )
            start += clip.duration_s
        mpd = dash_mod.build_mpd(
            periods,
            dynamic=not finished,
            publish_time=publish_time,
            availability_start_time=availability_start_time,
        )
        total_segments = len(flat[self.ladder[0].name].segments)
        return FilmManifest(
            format=StreamFormat.DASH,
            finished=finished,
            mpd=mpd,
            duration_s=self.duration_s,
            shot_count=len(self._clips),
            segment_count=total_segments,
        )


@dataclass(frozen=True)
class LiveWindow:
    """A sliding-window view onto a :class:`GrowingFilm` for live HLS playlists."""

    film: GrowingFilm
    kept_from: int
    media_sequence: int
    discontinuity_sequence: int

    @property
    def kept_clips(self) -> list[ShotClip]:
        return self.film.clips[self.kept_from :]

    def hls_media_playlist(self, rendition: Rendition) -> str:
        """The trimmed HLS media playlist for one rendition over the live window.

        Segments before ``kept_from`` are dropped; the retained segments keep
        their global sequence numbers, and the playlist declares the correct
        ``MEDIA-SEQUENCE`` + ``DISCONTINUITY-SEQUENCE``. The first retained
        segment is *not* re-marked as a discontinuity (its boundary slid out and
        is reflected in the discontinuity sequence instead).
        """
        track = self.film.rendition_track(rendition)
        kept_shot_ids = {c.shot_id for c in self.kept_clips}
        segments = [s for s in track.segments if s.shot_id in kept_shot_ids]
        if not segments:
            raise ManifestError("live window produced no segments")
        # The first retained segment must not carry a leading DISCONTINUITY (that
        # boundary is accounted for by discontinuity_sequence).
        first = segments[0]
        if first.discontinuity:
            segments = [first.model_copy(update={"discontinuity": False}), *segments[1:]]
        return hls_mod.build_media_playlist(
            segments,
            init_uri=track.init.uri,
            init_byte_range=track.init.byte_range.hls if track.init.byte_range else None,
            finished=False,
            media_sequence=self.media_sequence,
            discontinuity_sequence=self.discontinuity_sequence,
        )
