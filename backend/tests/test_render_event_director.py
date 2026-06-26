"""Event Director (Agent 1) — event script planning + concurrent fan-out + stitch.

An **event** is a beat-cluster (e.g. "the chase across the bridge") bundled into
ONE continuous vertical film. These tests cover the three layers:

* **planning** — :func:`plan_event_script` is pure: it clusters beats into an
  ordered shot list with per-beat durations (NOT a fixed constant), chains the
  §9.3 render modes (establishing → continuation), and emits explicit continuity
  hand-off (the end-state of shot N anchors the start of N+1 via its last frame);
* **fan-out** — :class:`EventDirector` renders the shots *concurrently*
  (``asyncio.gather``), proven by overlapping start timestamps on a 3-shot event;
* **stitch (golden)** — three real Ken-Burns clips stitch into ONE 720×1280 mp4
  whose merged sync map's last ``video_end_s`` equals the film duration ±1 frame.
"""

from __future__ import annotations

import time

import anyio
import pytest

from app.agents.contracts import Beat, RenderMode, SourceSpan
from app.render import degrade
from app.render.event_director import (
    EventDirector,
    EventShot,
    RenderedShot,
    plan_event_script,
)
from tests.test_render_support import (
    FakeObjectStore,
    make_slice,
    png_bytes,
    real_mp4,
    wav_bytes,
)

ffmpeg_only = pytest.mark.skipif(
    not degrade.ffmpeg_available(), reason="no ffmpeg binary available"
)


def _bridge_beats() -> list[Beat]:
    """The 'chase across the bridge' event: 3 beats of different density/pacing."""
    return [
        Beat(
            beat_id="b0",
            scene_id="scene_005",
            beat_index=0,
            summary="A wide stone bridge at dusk, fog rolling low over the water.",
            mood="calm, still",
            source_span=SourceSpan(page=12, word_range=(100, 140)),
        ),
        Beat(
            beat_id="b1",
            scene_id="scene_005",
            beat_index=1,
            summary=(
                "She sprints across the planks, boots pounding, breath ragged, the "
                "pursuers gaining behind her in a frantic, headlong chase."
            ),
            mood="tense chase",
            source_span=SourceSpan(page=12, word_range=(141, 180)),
        ),
        Beat(
            beat_id="b2",
            scene_id="scene_005",
            beat_index=2,
            summary="She reaches the far side and turns to face them.",
            mood="resolute",
            source_span=SourceSpan(page=13, word_range=(181, 205)),
        ),
    ]


# --------------------------------------------------------------------------- #
# WS1 — planning (pure, no ffmpeg)
# --------------------------------------------------------------------------- #


def test_plan_event_script_clusters_beats_into_ordered_shots() -> None:
    script = plan_event_script(
        event_id="evt_001",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(),
    )
    assert script.event_id == "evt_001"
    assert script.scene_id == "scene_005"
    assert len(script.shots) == 3  # 3 beats → 3 shots (within the 3–6 band)
    assert [s.ordinal for s in script.shots] == [0, 1, 2]
    assert [s.beat_id for s in script.shots] == ["b0", "b1", "b2"]


def test_plan_event_script_durations_are_per_beat_not_constant() -> None:
    """Clip length is decided by the event director from beat density + pacing —
    a calm wide beat lingers, a frantic chase tightens — never a fixed constant."""
    script = plan_event_script(
        event_id="evt_001", book_id="book_demo", scene_id="scene_005", beats=_bridge_beats()
    )
    durs = [s.duration_s for s in script.shots]
    assert all(3.0 <= d <= 8.0 for d in durs)  # the 3–8s band
    assert len(set(durs)) > 1  # genuinely per-beat, not one global constant
    # The calm/still establishing beat lingers longer than the frantic chase beat.
    assert script.shots[0].duration_s > script.shots[1].duration_s


def test_plan_event_script_chains_render_modes_and_handoff() -> None:
    """§9.3 chaining: a locked character establishes (reference_to_video) then the
    film *continues* from each accepted endpoint (video_continuation); the explicit
    hand-off anchors shot N+1 to shot N's last frame (lastframes/{book}/{shot})."""
    script = plan_event_script(
        event_id="evt_001",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(),  # one locked-reference character
    )
    # The full §9.3 chain in one event: establish the locked character, *continue*
    # from the accepted endpoint, then *land* the final pose on an exact frame.
    assert script.shots[0].render_mode == RenderMode.REFERENCE_TO_VIDEO  # establish
    assert script.shots[1].render_mode == RenderMode.VIDEO_CONTINUATION  # continue
    assert script.shots[2].render_mode == RenderMode.FIRST_LAST_FRAME  # "turns to face" pose
    # Explicit hand-off: each shot continues from the previous shot's last frame.
    assert script.shots[1].directive.continues_from_shot_id == script.shots[0].shot_id
    assert (
        script.shots[1].directive.last_frame_key
        == f"lastframes/book_demo/{script.shots[0].shot_id}.png"
    )
    # Every shot carries an explicit end-state hand-off for the next shot to open on.
    assert all(s.directive.hand_off for s in script.shots)


def test_plan_event_script_establishing_with_no_character_is_text_to_video() -> None:
    script = plan_event_script(
        event_id="evt_x", book_id="book_demo", scene_id="scene_005", beats=_bridge_beats()
    )  # no canon → no locked character
    assert script.shots[0].render_mode == RenderMode.TEXT_TO_VIDEO


def test_plan_event_script_first_shot_continues_from_prior_event_endpoint() -> None:
    """Cross-event continuity: shot 0 anchors to the canon's previous endpoint frame
    (the last accepted shot of the prior event) when one exists (§9.6)."""
    script = plan_event_script(
        event_id="evt_002",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(with_endpoint=True),  # previous_endpoint = shot_prev
    )
    assert script.shots[0].directive.continues_from_shot_id == "shot_prev"
    assert script.shots[0].directive.last_frame_key == "lastframes/book_demo/shot_prev.png"


def test_plan_event_script_caps_shots_at_max() -> None:
    beats = [
        Beat(beat_id=f"b{i}", scene_id="s", beat_index=i, summary=f"beat {i}")
        for i in range(9)
    ]
    script = plan_event_script(
        event_id="evt", book_id="book_demo", scene_id="s", beats=beats, max_shots=6
    )
    assert len(script.shots) == 6  # capped at the 6-shot ceiling


# --------------------------------------------------------------------------- #
# WS1 — concurrent fan-out
# --------------------------------------------------------------------------- #


class _SlowRenderer:
    """Records each shot's wall-clock window over a fixed sleep + a real clip.

    The real (cached) clip lets the orchestrator's stitch run; the sleep makes the
    overlap deterministic — under ``asyncio.gather`` every render starts before any
    finishes, which is the fan-out guarantee being asserted.
    """

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        started = time.monotonic()
        await anyio.sleep(0.05)
        return RenderedShot(
            shot_id=shot.shot_id,
            ordinal=shot.ordinal,
            clip_bytes=real_mp4(0.5, with_audio=True),
            last_frame_bytes=None,
            duration_s=shot.duration_s,
            render_mode=shot.render_mode,
            started_at=started,
            finished_at=time.monotonic(),
        )


@ffmpeg_only
async def test_event_director_fans_out_shots_concurrently() -> None:
    script = plan_event_script(
        event_id="evt", book_id="book_demo", scene_id="s", beats=_bridge_beats()
    )
    result = await EventDirector(_SlowRenderer()).render_event(script)
    assert result.shot_count == 3
    starts = [r.started_at for r in result.rendered]
    finishes = [r.finished_at for r in result.rendered]
    # Every shot started before any shot finished → the renders genuinely overlap.
    assert max(starts) < min(finishes)


# --------------------------------------------------------------------------- #
# WS2 — stitch (the DoD golden test)
# --------------------------------------------------------------------------- #


@ffmpeg_only
async def test_event_director_stitches_three_ken_burns_into_one_vertical_film() -> None:
    """DoD #2: a 3-shot Ken-Burns event renders concurrently and stitches into ONE
    720×1280 mp4 with a valid sync map — the last segment's ``video_end_s`` equals
    the film duration within one frame, and the film + per-shot last-frame anchors
    are persisted to the object store."""
    from app.storage.object_store import keys

    script = plan_event_script(
        event_id="evt_001",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(),
    )
    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}
    audio = {s.shot_id: wav_bytes(2.0) for s in script.shots}
    store = FakeObjectStore()

    result = await EventDirector(store=store).render_event(script, stills=stills, audio=audio)

    # ONE vertical 720×1280 film, playable, with audio.
    info = degrade.probe(result.clip_bytes)
    assert (info.width, info.height) == (720, 1280)
    assert info.has_video is True and info.has_audio is True
    assert degrade.verify_playable(result.clip_bytes) is True
    assert result.shot_count == 3

    # The stitched film's measured length matches the planned/merged duration, and
    # the sync map's last segment ends exactly at the map duration (±1 frame).
    assert abs(info.duration_s - result.duration_s) < 0.5
    one_frame = 1.0 / degrade.DEFAULT_FPS
    last = result.sync_map.segments[-1]
    assert abs(last.video_end_s - result.sync_map.duration_s) <= one_frame + 1e-6
    assert result.sync_map.scene_id == "evt_001"
    assert len(result.sync_map.segments) == 3

    # Persisted: the event film + every shot's last frame (continuation anchors).
    assert result.clip_key == keys.clip("book_demo", "evt_001")
    assert store.exists(result.clip_key)
    assert len(result.last_frame_keys) == 3
    for shot in script.shots:
        assert store.exists(keys.lastframe("book_demo", shot.shot_id))
