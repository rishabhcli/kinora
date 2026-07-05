"""Scene stitch (kinora.md §9.6): real ffmpeg concat + cumulative sync-map merge.

Two real degrade-produced mp4s concatenate into one valid mp4, and the per-shot
sync segments merge into a scene map whose video-times / word-times / page-turns
are shifted by the cumulative offset of the preceding shots.
"""

from __future__ import annotations

import pytest

from app.render import degrade
from app.render.stitch import concat_clips, merge_sync_segments
from app.render.sync_map import SyncSegment, SyncWord
from tests.test_render_support import png_bytes, wav_bytes

pytestmark = pytest.mark.skipif(not degrade.ffmpeg_available(), reason="no ffmpeg binary available")


def test_concat_two_clips_into_one_valid_mp4() -> None:
    clip_a = degrade.ken_burns_over_image(png_bytes(640, 360), 1.0, audio_bytes=wav_bytes(1.0))
    clip_b = degrade.ken_burns_over_image(png_bytes(640, 360), 1.5)  # no audio → silence padded
    result = concat_clips([clip_a, clip_b], size=(640, 360))
    info = degrade.probe(result.clip_bytes)
    assert info.has_video is True
    assert info.has_audio is True  # uniform audio layout (silence where missing)
    assert abs(info.duration_s - 2.5) < 0.4
    assert degrade.verify_playable(result.clip_bytes) is True
    # The reported durations are the REAL post-normalization probe, not the
    # (absent, here) caller estimate — must feed merge_sync_segments, not a
    # separately re-derived guess (kinora QA-campaign finding, 2026-07-05).
    assert len(result.durations) == 2
    assert all(d > 0 for d in result.durations)
    assert result.crossfade_s == 0.0  # no crossfade requested


def test_concat_enforces_vertical_film_geometry() -> None:
    """Kinora films are **vertical 720x1280** (short-drama format), so the offline
    scene stitch must concatenate a scene's shots into one 720x1280 mp4 whose
    duration is the sum of the shots'. The bug this guards: ``concat_clips`` used
    to *infer* geometry from the first clip and fall back to landscape 1920x1080,
    which leaked a landscape (or letterboxed) film. With no ``size`` override the
    stitch now enforces the vertical film geometry — a non-film source clip is
    scaled+padded into vertical, never leaked as landscape. Fully offline (ffmpeg
    only, no model/network)."""
    assert degrade.FILM_SIZE == (720, 1280)  # the canonical vertical film geometry
    # Landscape source shots of different lengths — the exact shape that used to
    # leak a landscape stitch through the inferred-size path.
    shot_a = degrade.ken_burns_over_image(png_bytes(640, 360), 2.0, audio_bytes=wav_bytes(2.0))
    shot_b = degrade.ken_burns_over_image(png_bytes(640, 360), 3.0)  # silence-padded

    # No size override → the stitcher's production path (now vertical, not inferred).
    result = concat_clips([shot_a, shot_b])

    info = degrade.probe(result.clip_bytes)
    assert (info.width, info.height) == (720, 1280)  # vertical 720x1280, NOT 1920x1080
    assert info.has_video is True and info.has_audio is True
    assert abs(info.duration_s - 5.0) < 0.4  # 2s + 3s combined
    assert degrade.verify_playable(result.clip_bytes) is True


def test_concat_keeps_full_duration_when_audio_shorter_and_no_ffprobe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: degraded clips mux narration that is *shorter* than the video,
    and the production render image has no ``ffprobe``. The stitcher used to call
    ``probe`` directly, which raised, so ``_normalize_segment`` mis-read the clips
    as audio-less and truncated each to ~0.1s — the stitched scene collapsed to a
    fraction of a second. With the ffprobe-free ``inspect`` fallback the concat
    must preserve the full combined duration."""
    # 4s video with only 2s of narration — the real degraded-clip shape.
    clip_a = degrade.ken_burns_over_image(png_bytes(640, 360), 4.0, audio_bytes=wav_bytes(2.0))
    clip_b = degrade.ken_burns_over_image(png_bytes(640, 360), 4.0, audio_bytes=wav_bytes(2.0))

    monkeypatch.setattr(degrade, "get_ffprobe_exe", lambda: None)  # the prod container
    result = concat_clips([clip_a, clip_b], size=(640, 360))

    assert degrade.verify_playable(result.clip_bytes) is True
    info = degrade.inspect(result.clip_bytes)
    assert info.has_video is True and info.has_audio is True
    assert abs(info.duration_s - 8.0) < 0.5  # full 4+4, not a truncated 0.2s


def test_merge_sync_segments_has_cumulative_timestamps() -> None:
    seg_a = SyncSegment(
        shot_id="a",
        video_start_s=0.0,
        video_end_s=1.0,
        page=1,
        page_turn_at_s=0.8,
        words=[SyncWord(word_index=1, text="hi", t_start=0.1, t_end=0.5, bbox=None)],
    )
    seg_b = SyncSegment(
        shot_id="b",
        video_start_s=0.0,
        video_end_s=1.5,
        page=2,
        page_turn_at_s=1.3,
        words=[SyncWord(word_index=2, text="bye", t_start=0.2, t_end=0.9, bbox=None)],
    )
    merged = merge_sync_segments([seg_a, seg_b], scene_id="scene_1", durations=[1.0, 1.5])

    assert merged.scene_id == "scene_1"
    assert merged.duration_s == 2.5
    assert (merged.segments[0].video_start_s, merged.segments[0].video_end_s) == (0.0, 1.0)
    assert (merged.segments[1].video_start_s, merged.segments[1].video_end_s) == (1.0, 2.5)
    # Second shot's page-turn + word timings shifted by the first shot's length.
    assert merged.segments[1].page_turn_at_s == pytest.approx(2.3)
    assert merged.segments[1].words[0].t_start == pytest.approx(1.2)
    assert merged.segments[1].words[0].t_end == pytest.approx(1.9)


def test_merge_uses_segment_length_when_durations_omitted() -> None:
    seg = SyncSegment(
        shot_id="only",
        video_start_s=0.0,
        video_end_s=4.0,
        page=1,
        page_turn_at_s=3.8,
        words=[],
    )
    merged = merge_sync_segments([seg, seg], scene_id="s")
    assert merged.duration_s == 8.0
    assert merged.segments[1].video_start_s == 4.0


def test_merge_sync_segments_accounts_for_crossfade_overlap() -> None:
    """When the stitch crossfades by ``overlap_s`` per seam, each shot starts that
    much *earlier* (it overlaps the prior shot's tail), so the merged timeline
    shrinks by ``(n-1)·overlap`` and the timecodes stay exact across the join."""

    def seg(shot_id: str) -> SyncSegment:
        return SyncSegment(
            shot_id=shot_id,
            video_start_s=0.0,
            video_end_s=3.0,
            page=1,
            page_turn_at_s=2.8,
            words=[SyncWord(word_index=1, text="x", t_start=0.5, t_end=1.0, bbox=None)],
        )

    merged = merge_sync_segments(
        [seg("a"), seg("b"), seg("c")], scene_id="s", durations=[3.0, 3.0, 3.0], overlap_s=0.5
    )
    assert merged.duration_s == 8.0  # 9 − 2×0.5
    assert merged.segments[0].video_start_s == 0.0
    assert merged.segments[1].video_start_s == 2.5  # 3.0 − 0.5
    assert merged.segments[2].video_start_s == 5.0  # 5.5 − 0.5
    assert merged.segments[-1].video_end_s == 8.0
    # Word timings ride the same overlap-aware shift as their segment.
    assert merged.segments[1].words[0].t_start == pytest.approx(3.0)  # 2.5 + 0.5


def test_concat_with_crossfade_overlaps_and_stays_vertical() -> None:
    """The cinematic event stitch crossfades video + audio between shots: ONE
    vertical 720×1280 mp4, playable, whose length is the sum minus the overlaps
    (no black frames, no aspect jump)."""
    clips = [
        degrade.ken_burns_over_image(
            png_bytes(720, 1280), 3.0, audio_bytes=wav_bytes(3.0), size=(720, 1280)
        )
        for _ in range(3)
    ]
    result = concat_clips(clips, crossfade_s=0.5)
    info = degrade.probe(result.clip_bytes)
    assert (info.width, info.height) == (720, 1280)
    assert info.has_video is True and info.has_audio is True
    assert abs(info.duration_s - 8.0) < 0.5  # 9s − 2×0.5s crossfade
    assert degrade.verify_playable(result.clip_bytes) is True
    # The reported crossfade is what was ACTUALLY applied (the real thing a
    # caller must feed merge_sync_segments' overlap_s), not the raw request.
    assert result.crossfade_s == pytest.approx(0.5)
    assert len(result.durations) == 3
    assert all(d > 0 for d in result.durations)


def test_concat_falls_back_to_expected_duration_on_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (kinora QA-campaign finding, 2026-07-05): when the real
    post-normalization probe fails for a clip, the reported duration must
    fall back to the caller's own expected_durations estimate (matching
    today's behavior for that shot) rather than silently reporting 0 and
    collapsing that shot's window in the merged sync map."""
    from app.render import stitch as stitch_module

    clip_a = degrade.ken_burns_over_image(png_bytes(640, 360), 2.0, audio_bytes=wav_bytes(2.0))
    clip_b = degrade.ken_burns_over_image(png_bytes(640, 360), 3.0, audio_bytes=wav_bytes(3.0))

    real_safe_duration = stitch_module._safe_duration
    calls = {"n": 0}

    def flaky_safe_duration(clip: bytes) -> float:
        calls["n"] += 1
        if calls["n"] == 1:
            return 0.0  # simulate a probe failure for the first clip only
        return real_safe_duration(clip)

    monkeypatch.setattr(stitch_module, "_safe_duration", flaky_safe_duration)

    result = concat_clips([clip_a, clip_b], size=(640, 360), expected_durations=[2.0, 3.0])

    assert result.durations[0] == 2.0  # fell back to the caller's estimate
    assert result.durations[1] > 0  # the second clip's real probe succeeded
    assert degrade.verify_playable(result.clip_bytes) is True
