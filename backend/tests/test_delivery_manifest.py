"""Growing/live manifest correctness — segment math, discontinuities, live window."""

from __future__ import annotations

import xml.dom.minidom as minidom

import pytest

from app.delivery.errors import ManifestError
from app.delivery.manifest import DEFAULT_SEGMENT_DURATION_S, GrowingFilm, UriScheme
from app.delivery.models import ShotClip


def _clip(shot: str, order: int, dur: float, provider: str = "wan") -> ShotClip:
    return ShotClip(
        shot_id=shot,
        order=order,
        duration_s=dur,
        provider=provider,
        source_key=f"k/{shot}",
        width=720,
        height=1280,
    )


def _film(*clips: ShotClip, seg: float = DEFAULT_SEGMENT_DURATION_S) -> GrowingFilm:
    return GrowingFilm.from_clips(book_id="b", clips=list(clips), segment_duration_s=seg)


# -- segment math ---------------------------------------------------------- #


def test_segment_durations_sum_to_shot_duration_exactly() -> None:
    clip = _clip("s1", 0, 5.0)
    # 5.0s @ 2.0s segments → [2,2,1].
    assert clip.segment_durations(2.0) == [2.0, 2.0, 1.0]
    assert abs(sum(clip.segment_durations(2.0)) - 5.0) < 1e-9


def test_segment_durations_exact_multiple_no_short_tail() -> None:
    clip = _clip("s1", 0, 6.0)
    assert clip.segment_durations(2.0) == [2.0, 2.0, 2.0]


def test_segment_durations_shorter_than_one_segment() -> None:
    clip = _clip("s1", 0, 1.3)
    assert clip.segment_durations(2.0) == [1.3]
    assert clip.segment_count(2.0) == 1


def test_track_total_duration_matches_film() -> None:
    film = _film(_clip("s1", 0, 5.0), _clip("s2", 1, 3.0))
    track = film.rendition_track(film.ladder[0])
    assert abs(track.duration_s - film.duration_s) < 1e-6
    assert film.duration_s == 8.0


# -- discontinuities at shot boundaries ------------------------------------ #


def test_discontinuity_marked_only_at_shot_boundaries() -> None:
    film = _film(_clip("s1", 0, 5.0), _clip("s2", 1, 3.0), _clip("s3", 2, 2.0))
    segs = film.rendition_track(film.ladder[0]).segments
    disc = [(s.sequence, s.shot_id) for s in segs if s.discontinuity]
    # First segment of s2 and s3 are discontinuities; nothing inside a shot.
    assert disc == [(3, "s2"), (5, "s3")]
    # The very first segment of the film is never a discontinuity.
    assert segs[0].discontinuity is False


def test_media_sequence_monotonic_across_shots() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 4.0))
    seqs = [s.sequence for s in film.rendition_track(film.ladder[0]).segments]
    assert seqs == list(range(len(seqs)))


def test_hls_media_playlist_emits_discontinuity_tag() -> None:
    film = _film(_clip("s1", 0, 3.0), _clip("s2", 1, 3.0))
    media = film.build_hls(finished=True).media[film.ladder[0].name]
    assert media.count("#EXT-X-DISCONTINUITY\n") == 1
    assert "#EXT-X-ENDLIST" in media  # finished → VOD
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in media


def test_hls_growing_playlist_has_no_endlist() -> None:
    film = _film(_clip("s1", 0, 3.0))
    media = film.build_hls(finished=False).media[film.ladder[0].name]
    assert "#EXT-X-ENDLIST" not in media
    assert "#EXT-X-PLAYLIST-TYPE:EVENT" in media


def test_hls_target_duration_is_ceiling_of_longest_segment() -> None:
    film = _film(_clip("s1", 0, 2.4), seg=2.0)  # segments [2.0, 0.4]
    media = film.build_hls(finished=True).media[film.ladder[0].name]
    assert "#EXT-X-TARGETDURATION:2" in media


def test_hls_master_lists_every_rendition_highest_first() -> None:
    film = _film(_clip("s1", 0, 3.0))
    master = film.build_hls(finished=True).master
    assert master is not None
    inf_lines = [ln for ln in master.splitlines() if ln.startswith("#EXT-X-STREAM-INF")]
    assert len(inf_lines) == len(film.ladder)
    # First STREAM-INF is the highest bandwidth.
    bandwidths = [int(ln.split("BANDWIDTH=")[1].split(",")[0]) for ln in inf_lines]
    assert bandwidths == sorted(bandwidths, reverse=True)


# -- growing film append --------------------------------------------------- #


def test_append_grows_the_manifest() -> None:
    film = _film(_clip("s1", 0, 4.0))
    before = len(film.rendition_track(film.ladder[0]).segments)
    film.append(_clip("s2", 1, 4.0))
    after = len(film.rendition_track(film.ladder[0]).segments)
    assert after == before + 2  # 4s @ 2s = 2 more segments
    assert film.duration_s == 8.0


def test_append_requires_increasing_order() -> None:
    film = _film(_clip("s1", 1, 4.0))
    with pytest.raises(ManifestError):
        film.append(_clip("s2", 1, 4.0))  # not strictly increasing
    with pytest.raises(ManifestError):
        film.append(_clip("s0", 0, 4.0))


# -- live window (sliding) ------------------------------------------------- #


def test_live_window_keeps_recent_shots_and_slides_sequences() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 4.0), _clip("s3", 2, 4.0))
    # 3 shots * 2 segments each = 6 segments; keep the last shot only.
    window = film.live_window(max_shots=1)
    assert [c.shot_id for c in window.kept_clips] == ["s3"]
    # Media sequence of first kept segment = 4 segments before it.
    assert window.media_sequence == 4
    # 2 shot boundaries slid out of the window front.
    assert window.discontinuity_sequence == 2


def test_live_window_playlist_drops_leading_discontinuity() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 4.0))
    window = film.live_window(max_shots=1)
    playlist = window.hls_media_playlist(film.ladder[0])
    # The first kept segment was a discontinuity boundary — but in a sliding
    # window the boundary is reflected in DISCONTINUITY-SEQUENCE, not a tag.
    assert "#EXT-X-DISCONTINUITY-SEQUENCE:1" in playlist
    assert "#EXT-X-MEDIA-SEQUENCE:2" in playlist
    # No leading #EXT-X-DISCONTINUITY before the first segment line.
    lines = playlist.splitlines()
    map_idx = next(i for i, ln in enumerate(lines) if ln.startswith("#EXT-X-MAP"))
    first_extinf = next(i for i, ln in enumerate(lines) if ln.startswith("#EXTINF"))
    assert "#EXT-X-DISCONTINUITY" not in lines[map_idx + 1 : first_extinf]


def test_live_window_by_duration() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 4.0), _clip("s3", 2, 4.0))
    # Window of ~5s should retain only the last shot (or last two if they fit).
    window = film.live_window(max_duration_s=5.0)
    retained = sum(c.duration_s for c in window.kept_clips)
    assert retained <= 5.0 + 4.0  # at most one extra shot's worth over the bound
    assert len(window.kept_clips) >= 1


def test_live_window_no_bound_keeps_whole_film() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 4.0))
    window = film.live_window()
    assert len(window.kept_clips) == 2
    assert window.media_sequence == 0
    assert window.discontinuity_sequence == 0


def test_live_window_empty_film_raises() -> None:
    film = GrowingFilm.from_clips(
        book_id="b",
        clips=[],
        ladder=GrowingFilm.from_clips(book_id="b", clips=[_clip("s", 0, 2.0)]).ladder,
    )
    with pytest.raises(ManifestError):
        film.live_window()


# -- DASH ------------------------------------------------------------------ #


def test_dash_one_period_per_shot_and_wellformed() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 3.0))
    mpd = film.build_dash(finished=True).mpd
    assert mpd is not None
    minidom.parseString(mpd)  # must be well-formed XML
    assert mpd.count("<Period ") == 2
    assert 'type="static"' in mpd
    assert 'mediaPresentationDuration="PT7S"' in mpd


def test_dash_dynamic_requires_anchors() -> None:
    film = _film(_clip("s1", 0, 4.0))
    with pytest.raises(ManifestError):
        film.build_dash(finished=False)  # missing publishTime/availabilityStartTime
    ok = film.build_dash(
        finished=False,
        publish_time="2026-06-30T00:00:00Z",
        availability_start_time="2026-06-30T00:00:00Z",
    )
    assert ok.mpd is not None
    assert 'type="dynamic"' in ok.mpd
    minidom.parseString(ok.mpd)


def test_dash_period_durations_match_shots() -> None:
    film = _film(_clip("s1", 0, 4.0), _clip("s2", 1, 3.0))
    mpd = film.build_dash(finished=True).mpd
    assert mpd is not None
    assert 'start="PT0S" duration="PT4S"' in mpd
    assert 'start="PT4S" duration="PT3S"' in mpd


def test_dash_segmenturl_durations_sum_per_shot() -> None:
    film = _film(_clip("s1", 0, 5.0))  # [2,2,1] → d=2000,2000,1000
    mpd = film.build_dash(finished=True).mpd
    assert mpd is not None
    assert 'd="2000"' in mpd and 'd="1000"' in mpd


# -- URI scheme injection -------------------------------------------------- #


def test_custom_uri_scheme_is_used_for_segments() -> None:
    scheme = UriScheme(
        media_playlist=lambda name: f"https://cdn/{name}/list.m3u8",
        init_segment=lambda name: f"https://cdn/{name}/i.mp4",
        media_segment=lambda name, order, idx, seq: f"https://cdn/{name}/{seq}.m4s",
    )
    film = GrowingFilm.from_clips(book_id="b", clips=[_clip("s1", 0, 3.0)], uri_scheme=scheme)
    media = film.build_hls(finished=True).media[film.ladder[0].name]
    assert "https://cdn/720p/i.mp4" in media
    assert "https://cdn/720p/0.m4s" in media


def test_hls_master_signer_applied_to_media_uris() -> None:
    film = _film(_clip("s1", 0, 3.0))
    signed = film.build_hls(finished=True, signer=lambda u: f"{u}?sig=abc")
    assert signed.master is not None
    assert "?sig=abc" in signed.master


def test_rendition_track_rejects_foreign_rendition() -> None:
    from app.delivery.ladder import Rendition

    film = _film(_clip("s1", 0, 3.0))
    foreign = Rendition(name="x", width=10, height=10, video_bitrate_kbps=1, max_bitrate_kbps=2)
    with pytest.raises(ManifestError):
        film.rendition_track(foreign)
