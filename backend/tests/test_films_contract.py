"""Pure unit tests for the Agent-03 film contract (no DB / network).

These cover the wire-shape conversion + cumulative merge that turn Agent 1's
per-shot sync segments (§9.4) into the published ``FilmSyncMap`` (CONTRACTS.md
§Agent-03) and the ``event_stitched`` / ``scene_stitched`` SSE payloads (§5.6).
"""

from __future__ import annotations

from app.films.contract import (
    EventFilm,
    FilmSyncMap,
    FilmSyncSegment,
    SyncWord,
    event_stitched_event,
    film_sync_map_from_merged,
    merge_and_build_film_sync_map,
    scene_stitched_event,
)

# A per-shot segment is 0-based on its own timeline (what build_sync_segment emits).
SHOT_A = {
    "shot_id": "shot_A",
    "video_start_s": 0.0,
    "video_end_s": 5.0,
    "page": 12,
    "page_turn_at_s": 4.8,
    "words": [
        {"word_index": 4501, "text": "She", "t_start": 0.1, "t_end": 0.32,
         "bbox": [0.12, 0.34, 0.04, 0.02]},
        {"word_index": 4502, "text": "stood", "t_start": 0.32, "t_end": 0.61, "bbox": None},
    ],
}
SHOT_B = {
    "shot_id": "shot_B",
    "video_start_s": 0.0,
    "video_end_s": 3.0,
    "page": 13,
    "page_turn_at_s": 2.8,
    "words": [
        {"word_index": 4540, "text": "He", "t_start": 0.0, "t_end": 0.5, "bbox": None},
    ],
}
SPANS = {"shot_A": (4501, 4530), "shot_B": (4531, 4560)}


def test_merge_assigns_cumulative_film_timeline() -> None:
    """Second shot is shifted onto the scene timeline by the first shot's duration."""
    film_map = merge_and_build_film_sync_map(
        [SHOT_A, SHOT_B], scene_id="scene_005", spans=SPANS, durations=[5.0, 3.0]
    )
    assert isinstance(film_map, FilmSyncMap)
    assert film_map.scene_id == "scene_005"
    assert film_map.duration_s == 8.0
    a, b = film_map.segments
    # shot_A stays at the origin.
    assert (a.t_start_s, a.t_end_s) == (0.0, 5.0)
    # shot_B is shifted by 5.0s (video_start/end, page-turn, and every word).
    assert (b.t_start_s, b.t_end_s) == (5.0, 8.0)
    assert b.page_turn_at_s == 7.8
    assert b.words[0].t_start == 5.0
    assert b.words[0].t_end == 5.5


def test_segments_carry_mission_core_fields() -> None:
    """Every segment exposes {shot_id, scene_id, word_range, t_start_s, t_end_s}."""
    film_map = merge_and_build_film_sync_map(
        [SHOT_A, SHOT_B], scene_id="scene_005", spans=SPANS, durations=[5.0, 3.0]
    )
    a, b = film_map.segments
    assert isinstance(a, FilmSyncSegment)
    assert a.shot_id == "shot_A"
    assert a.scene_id == "scene_005"
    assert tuple(a.word_range) == (4501, 4530)
    assert b.word_range == (4531, 4560)
    # §9.4 enrichment preserved.
    assert a.page == 12
    assert isinstance(a.words[0], SyncWord)
    assert a.words[0].word_index == 4501
    assert a.words[0].bbox == [0.12, 0.34, 0.04, 0.02]
    assert a.words[1].bbox is None


def test_missing_span_defaults_to_zero_range() -> None:
    """A shot without a known span still produces a segment (degenerate range)."""
    film_map = merge_and_build_film_sync_map(
        [SHOT_A], scene_id="scene_005", spans={}, durations=None
    )
    assert film_map.segments[0].word_range == (0, 0)


def test_film_sync_map_from_merged_does_not_reshift() -> None:
    """Converting an already-merged scene map only renames fields (no extra shift)."""
    merged = {
        "scene_id": "scene_005",
        "duration_s": 8.0,
        "segments": [
            {**SHOT_B, "video_start_s": 5.0, "video_end_s": 8.0, "page_turn_at_s": 7.8},
        ],
    }
    film_map = film_sync_map_from_merged(merged, scene_id="scene_005", spans=SPANS)
    seg = film_map.segments[0]
    assert (seg.t_start_s, seg.t_end_s) == (5.0, 8.0)
    assert seg.page_turn_at_s == 7.8
    assert seg.scene_id == "scene_005"
    assert seg.word_range == (4531, 4560)


def test_scene_stitched_event_payload() -> None:
    """scene_stitched rides §5.6 with a canonical FilmSyncMap."""
    film_map = merge_and_build_film_sync_map(
        [SHOT_A], scene_id="scene_005", spans=SPANS, durations=[5.0]
    )
    payload = scene_stitched_event(
        scene_id="scene_005",
        oss_url="http://minio:9000/kinora/clips/b/scene_005.mp4",
        sync_map=film_map,
    )
    assert payload["event"] == "scene_stitched"
    assert payload["scene_id"] == "scene_005"
    assert payload["oss_url"].endswith("scene_005.mp4")
    # sync_map is plain JSON (dict), ready to publish over redis/SSE.
    assert isinstance(payload["sync_map"], dict)
    assert payload["sync_map"]["segments"][0]["t_start_s"] == 0.0
    assert payload["sync_map"]["segments"][0]["scene_id"] == "scene_005"


def test_event_stitched_event_payload() -> None:
    """event_stitched is the event-level rollup (event == scene today)."""
    film_map = merge_and_build_film_sync_map(
        [SHOT_A], scene_id="scene_005", spans=SPANS, durations=[5.0]
    )
    payload = event_stitched_event(
        event_id="scene_005", oss_url="http://x/clips/b/scene_005.mp4", sync_map=film_map
    )
    assert payload["event"] == "event_stitched"
    assert payload["event_id"] == "scene_005"
    assert isinstance(payload["sync_map"], dict)


def test_event_film_serializes_word_range_as_array() -> None:
    """EventFilm round-trips to JSON with tuple word_range emitted as a 2-array."""
    film_map = merge_and_build_film_sync_map(
        [SHOT_A], scene_id="scene_005", spans=SPANS, durations=[5.0]
    )
    event = EventFilm(
        event_id="scene_005",
        event_index=5,
        book_id="book_1",
        page_start=12,
        page_end=12,
        word_range=(4501, 4530),
        stitched=True,
        oss_url="http://x/clips/book_1/scene_005.mp4",
        url_expires_at=None,
        duration_s=5.0,
        shot_count=1,
        sync_map=film_map,
        scenes=[],
    )
    dumped = event.model_dump(mode="json")
    assert dumped["word_range"] == [4501, 4530]
    assert dumped["sync_map"]["segments"][0]["word_range"] == [4501, 4530]
