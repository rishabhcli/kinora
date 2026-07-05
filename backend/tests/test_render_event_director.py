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
    KenBurnsEventRenderer,
    RenderedShot,
    _lighting_from,
    _setting_from,
    plan_event_script,
)
from tests.test_render_support import (
    FakeDefectRepo,
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


def test_plan_event_script_assigns_shot_grammar_and_screen_direction() -> None:
    """The plan carries §10 production grammar: an establishing wide, a pose insert,
    and screen direction held then flipped only on the motivated reversal."""
    script = plan_event_script(
        event_id="evt_001",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(),
    )
    sizes = [s.camera.shot_size for s in script.shots]
    assert sizes[0] == "wide"  # establishing
    assert sizes[-1] == "close"  # "turns to face" pose → close insert
    assert len(set(sizes)) > 1  # a real grammar, not one size repeated
    # b1 sprints across (L2R); b2 "turns to face" is a motivated reversal (R2L).
    assert script.shots[1].directive.screen_direction == "left_to_right"
    assert script.shots[2].directive.screen_direction == "right_to_left"
    assert script.shots[2].directive.motion_reversal is True


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


def test_plan_segment_script_packs_beats_into_le15s_segments() -> None:
    """The single-clip planner packs consecutive beats into ≤15s segments (one
    EventShot per segment), chaining a continuation off each segment's last frame —
    the bridge's b0+b1 share page 12 (one take), b2 on page 13 opens the next."""
    from app.render.event_director import plan_segment_script

    script = plan_segment_script(
        event_id="evt_seg",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(),  # one locked-reference character
    )
    assert [s.ordinal for s in script.shots] == [0, 1]
    assert [s.shot_id for s in script.shots] == ["scene_005_seg_00", "scene_005_seg_01"]
    assert all(s.duration_s <= 15.0 for s in script.shots)
    # One continuous take establishes; the next continues from its last frame.
    assert script.shots[0].render_mode == RenderMode.REFERENCE_TO_VIDEO
    assert script.shots[1].render_mode == RenderMode.VIDEO_CONTINUATION
    assert script.shots[1].directive.continues_from_shot_id == "scene_005_seg_00"
    assert (
        script.shots[1].directive.last_frame_key == "lastframes/book_demo/scene_005_seg_00.png"
    )
    # The first segment's prompt material spans BOTH its packed beats.
    assert "bridge" in script.shots[0].summary and "sprints" in script.shots[0].summary


def test_plan_segment_script_one_segment_when_scene_fits_in_15s() -> None:
    """A short same-page scene becomes ONE ≤15s take — no stitch needed."""
    from app.render.event_director import plan_segment_script

    beats = [
        Beat(
            beat_id=f"b{i}",
            scene_id="s",
            beat_index=i,
            summary="short",
            source_span=SourceSpan(page=1, word_range=(i * 5, i * 5 + 5)),
        )
        for i in range(2)
    ]
    script = plan_segment_script(event_id="e", book_id="bk", scene_id="s", beats=beats)
    assert len(script.shots) == 1
    assert script.shots[0].duration_s <= 15.0


def test_plan_event_script_caps_shots_at_max() -> None:
    beats = [
        Beat(beat_id=f"b{i}", scene_id="s", beat_index=i, summary=f"beat {i}") for i in range(9)
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
    # canon=make_slice(with_style=True) chains the modes (establish → continue →
    # land-pose) AND pins lighting to a style token (rather than each beat's own
    # mood — see _lighting_from's no-style fallback) so this fixture's WS3
    # continuity is genuinely clean on both gates; otherwise (no canon → every
    # shot text_to_video, never chained; no style → lighting drifts with every
    # beat's mood) the repair loop would legitimately fire and its sequential
    # re-render would overwrite the very overlapping timestamps this test
    # exists to prove — a different concern than fan-out concurrency. film_size
    # matches _SlowRenderer's real (non-FILM_SIZE) clips so geometry is clean.
    script = plan_event_script(
        event_id="evt",
        book_id="book_demo",
        scene_id="s",
        beats=_bridge_beats(),
        canon=make_slice(with_style=True),
    )
    result = await EventDirector(_SlowRenderer(), film_size=(1920, 1080)).render_event(script)
    assert result.continuity is not None and result.continuity.ok is True  # no repair fired
    assert result.shot_count == 3
    starts = [r.started_at for r in result.rendered]
    finishes = [r.finished_at for r in result.rendered]
    # Every shot started before any shot finished → the renders genuinely overlap.
    assert max(starts) < min(finishes)


class _RaisingRenderer:
    """One shot raises an unexpected exception instead of returning — a
    renderer's own contract is "never raise for a known provider issue,
    degrade internally instead" (see LiveEventShotRenderer's module
    docstring), but a bug or an unclassified exception type (e.g. a bare
    ValidationError no ``except (LiveVideoDisabled, ProviderError)`` guard
    catches) can still escape it. Records every shot_id it was called with so
    a test can prove every OTHER shot still ran to completion.

    Successful shots render at FILM_SIZE (matching the gather's own
    KenBurnsEventRenderer fallback) so the stitched event's geometry stays
    uniform and the SEPARATE continuity-repair loop never fires — this test
    stays scoped to the gather-level fix, not that other code path."""

    def __init__(self, *, fails_shot_id: str, exc: BaseException) -> None:
        self._fails_shot_id = fails_shot_id
        self._exc = exc
        self.calls: list[str] = []

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        self.calls.append(shot.shot_id)
        if shot.shot_id == self._fails_shot_id:
            raise self._exc
        assert still is not None
        clip = degrade.ken_burns_over_image(still, shot.duration_s, size=degrade.FILM_SIZE)
        return RenderedShot(
            shot_id=shot.shot_id,
            ordinal=shot.ordinal,
            clip_bytes=clip,
            last_frame_bytes=still,
            duration_s=shot.duration_s,
            render_mode=shot.render_mode,
        )


@ffmpeg_only
async def test_one_shot_raising_does_not_crash_the_whole_event() -> None:
    """Finding 3b (resilience audit): render_event's asyncio.gather had no
    return_exceptions=True, so one shot raising an unexpected exception
    cancelled every OTHER shot's already-in-flight render and crashed the
    whole event instead of costing only the one shot it hit."""
    # with_style=True keeps the continuity/persistence gates clean (see the
    # fan-out test above) so this test stays scoped to the gather-level fix,
    # not the separate _repair loop.
    script = plan_event_script(
        event_id="evt_raise",
        book_id="book_demo",
        scene_id="s",
        beats=_bridge_beats(),
        canon=make_slice(with_style=True),
    )
    failing_shot_id = script.shots[1].shot_id
    renderer = _RaisingRenderer(fails_shot_id=failing_shot_id, exc=ValueError("unexpected bug"))
    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}

    result = await EventDirector(renderer).render_event(script, stills=stills)

    assert renderer.calls == [s.shot_id for s in script.shots]  # every shot was attempted
    assert result.shot_count == 3
    failing_result = next(r for r in result.rendered if r.shot_id == failing_shot_id)
    assert failing_result.degraded is True
    assert degrade.verify_playable(failing_result.clip_bytes) is True  # real fallback clip
    others = [r for r in result.rendered if r.shot_id != failing_shot_id]
    assert len(others) == 2
    assert all(r.degraded is False for r in others)  # siblings shipped their real render


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

    # with_style=True pins lighting to a canon style token (rather than each
    # beat's own varying mood — see _lighting_from's no-style fallback) so this
    # fixture is genuinely clean on BOTH WS3 repair gates (geometry AND the
    # narrative persistence check) and the "no seam needs repair" claim below
    # is actually exercised, not just asserted.
    script = plan_event_script(
        event_id="evt_001",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats(),
        canon=make_slice(with_style=True),
    )
    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}
    audio = {s.shot_id: wav_bytes(2.0) for s in script.shots}
    store = FakeObjectStore()
    defects = FakeDefectRepo()

    result = await EventDirector(store=store, defect_repo=defects).render_event(
        script, stills=stills, audio=audio
    )

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

    # The integrated continuity QA passes: uniform vertical geometry, chained
    # modes, explicit hand-offs → no seam needs repair. Confirmed directly (not
    # just inferred from the geometry-only report): zero repairs were logged,
    # so the persistence gate stayed quiet too.
    from app.render.continuity_qa import SeamRepair

    assert result.continuity is not None
    assert result.continuity.ok is True
    assert result.continuity.action == SeamRepair.ACCEPT
    assert defects.logged == []


async def test_stitch_uses_concat_clips_own_durations_and_crossfade(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (kinora QA-campaign finding, 2026-07-05): _stitch used to
    pre-compute `overlap` from each shot's OWN reported duration_s (a
    pre-normalization estimate) and feed that same guess to both
    concat_clips' crossfade_s and merge_sync_segments' overlap_s. But
    concat_clips can compute a DIFFERENT real crossfade/durations internally
    (from its own post-normalization probe, e.g. after fps conversion or the
    silent-audio pad shift a clip's real length), so the two could silently
    diverge — drifting the sync map's per-shot boundaries (and the
    clip_start_s/clip_end_s seek windows derived from them, Task 8) away from
    what the real stitched video actually contains. _stitch must build the
    sync map from concat_clips' OWN returned durations/crossfade_s, not a
    separately pre-computed guess."""
    from app.render.stitch import ConcatResult

    script = plan_event_script(
        event_id="evt_stitch_source",
        book_id="book_demo",
        scene_id="s",
        beats=_bridge_beats(),
        canon=make_slice(with_style=True),
    )
    rendered = [
        RenderedShot(
            shot_id=s.shot_id,
            ordinal=s.ordinal,
            clip_bytes=b"unused",
            last_frame_bytes=None,
            duration_s=s.duration_s,
            render_mode=s.render_mode,
        )
        for s in script.shots
    ]

    # Deliberately DIFFERENT from what _stitch would get by pre-computing
    # from `rendered`'s own duration_s values — simulates normalization
    # shifting the real, post-encoding clip lengths.
    fake_result = ConcatResult(
        clip_bytes=b"THE_REAL_STITCHED_BYTES", durations=[9.0, 8.0, 7.0], crossfade_s=0.15
    )
    captured: dict[str, object] = {}

    def fake_concat_clips(*args: object, **kwargs: object) -> ConcatResult:
        captured["expected_durations"] = kwargs.get("expected_durations")
        captured["crossfade_s"] = kwargs.get("crossfade_s")
        return fake_result

    import app.render.event_director as event_director_module

    monkeypatch.setattr(event_director_module, "concat_clips", fake_concat_clips)

    director = EventDirector(crossfade_s=0.4)
    clip_bytes, sync_map = await director._stitch(
        script, rendered, page_boxes=None, word_timestamps=None
    )

    assert clip_bytes == fake_result.clip_bytes
    # concat_clips is handed the raw requested crossfade (0.4) — it, not
    # _stitch, decides the real, effective crossfade.
    assert captured["crossfade_s"] == 0.4
    # The merged timeline must be built from fake_result's OWN
    # durations/crossfade: 9+8+7 - 2*0.15 = 23.7 — not from `rendered`'s own
    # duration_s values (which would give a materially different total).
    assert sync_map.duration_s == pytest.approx(23.7)
    assert sync_map.segments[1].video_start_s == pytest.approx(8.85)  # 9.0 - 0.15


# --------------------------------------------------------------------------- #
# WS3 — the repair loop acts on continuity QA instead of only logging it
# --------------------------------------------------------------------------- #


class _CountingKenBurnsRenderer:
    """A real, uniform-FILM_SIZE Ken-Burns renderer (delegates to
    KenBurnsEventRenderer) that records every shot_id it renders — so a test
    can prove a repair actually re-rendered/inserted a shot, not just noted
    the continuity failure."""

    def __init__(self) -> None:
        self._inner = KenBurnsEventRenderer()
        self.calls: list[str] = []

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        self.calls.append(shot.shot_id)
        return await self._inner.render_shot(shot, still=still, audio=audio)


class _FixedGeometryRenderer:
    """Renders every shot as a REAL Ken-Burns mp4 at an explicit, per-shot
    pixel size (not the event's uniform FILM_SIZE) — used to force a genuine
    geometry MISMATCH between two shots' probed clip dimensions so continuity
    QA's geometry check fails deterministically, without fabricating
    unplayable clip bytes. Also records every shot_id it renders."""

    def __init__(self, sizes: dict[str, tuple[int, int]]) -> None:
        self._sizes = sizes
        self.calls: list[str] = []

    async def render_shot(
        self, shot: EventShot, *, still: bytes | None, audio: bytes | None
    ) -> RenderedShot:
        self.calls.append(shot.shot_id)
        size = self._sizes[shot.shot_id]
        clip = await anyio.to_thread.run_sync(
            lambda: degrade.ken_burns_over_image(
                still or png_bytes(*size), shot.duration_s, audio_bytes=audio, size=size
            )
        )
        return RenderedShot(
            shot_id=shot.shot_id,
            ordinal=shot.ordinal,
            clip_bytes=clip,
            last_frame_bytes=still,
            duration_s=shot.duration_s,
            render_mode=shot.render_mode,
        )


@ffmpeg_only
async def test_render_event_inserts_supplemental_shot_on_hard_cut() -> None:
    """A seam with no chained render mode (both shots plan as text_to_video —
    no canon means no locked character to establish/continue from — so the
    only seam is a hard, unchained cut even though the geometry matches)
    routes to INSERT_SUPPLEMENTAL — render_event must actually add and render
    the supplemental shot, not just note the failure."""
    script = plan_event_script(
        event_id="evt_repair",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats()[:2],  # no canon → both shots are text_to_video
    )
    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}
    renderer = _CountingKenBurnsRenderer()
    director = EventDirector(renderer)  # defect_repo defaults to None: must not crash

    result = await director.render_event(script, stills=stills)

    from app.render.continuity_qa import SeamRepair

    assert result.shot_count == 3  # original 2 + 1 supplemental inserted
    assert result.continuity is not None
    assert result.continuity.action == SeamRepair.INSERT_SUPPLEMENTAL
    # The supplemental shot was actually rendered, not just noted.
    supp_id = f"{script.shots[0].shot_id}_supp"
    assert renderer.calls.count(supp_id) == 1
    # The sync map reflects the REPAIRED shot list (original 2 + the inserted
    # supplemental), not a stale pre-repair stitch: same count as shot_count,
    # the supplemental lands between the two shots it bridges, and the
    # cumulative timeline starts at 0 and ends exactly at the map duration.
    assert len(result.sync_map.segments) == result.shot_count == 3
    assert [seg.shot_id for seg in result.sync_map.segments] == [
        script.shots[0].shot_id,
        supp_id,
        script.shots[1].shot_id,
    ]
    assert result.sync_map.segments[0].video_start_s == 0.0
    assert result.sync_map.segments[-1].video_end_s == result.sync_map.duration_s


async def test_render_event_regenerates_shot_on_geometry_only_failure() -> None:
    """A seam that is properly chained + handed off (canon present, so shot 1
    is a video_continuation of shot 0) but whose rendered clips land at
    mismatched resolutions routes to REGEN_CONTINUATION — render_event must
    re-render the later shot, not just note the failure."""
    script = plan_event_script(
        event_id="evt_regen",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats()[:2],
        # with_style=True: a locked-reference character chains + hands off shot
        # 1, AND pins lighting to a style token instead of each beat's own
        # (differing) mood, so persistence is clean and geometry is this
        # fixture's ONLY failure — see _lighting_from's no-style fallback.
        canon=make_slice(with_style=True),
    )
    shot0_id, shot1_id = script.shots[0].shot_id, script.shots[1].shot_id
    mismatched = _FixedGeometryRenderer({shot0_id: (720, 1280), shot1_id: (600, 800)})
    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}
    defects = FakeDefectRepo()
    director = EventDirector(mismatched, defect_repo=defects)

    result = await director.render_event(script, stills=stills)

    assert result.shot_count == 2  # regen re-renders in place, never inserts
    assert mismatched.calls.count(shot1_id) == 2  # rendered, then regenerated
    assert mismatched.calls.count(shot0_id) == 1  # the earlier shot is untouched
    assert len(defects.logged) == 1
    assert defects.logged[0]["kind"] == "seam_repair"
    assert defects.logged[0]["shot_id"] == shot1_id
    assert defects.logged[0]["detail"] == {"action": "regen_continuation"}


@ffmpeg_only
async def test_render_event_degrades_when_repair_route_is_degrade() -> None:
    """A seam with BOTH a geometry failure (mismatched per-shot resolutions)
    and a chain failure (no canon → both shots text_to_video, never chained)
    routes to DEGRADE — render_event must fall back to a fresh, real Ken-Burns
    render for every shot in the event rather than ship the known-bad seam."""
    script = plan_event_script(
        event_id="evt_degrade",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats()[:2],  # no canon → both shots text_to_video (chain fail)
    )
    shot0_id, shot1_id = script.shots[0].shot_id, script.shots[1].shot_id
    mismatched = _FixedGeometryRenderer({shot0_id: (720, 1280), shot1_id: (600, 800)})
    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}
    defects = FakeDefectRepo()
    director = EventDirector(mismatched, defect_repo=defects)

    result = await director.render_event(script, stills=stills)

    assert result.shot_count == 2  # DEGRADE re-renders in place, never inserts
    assert len(defects.logged) == 1
    assert defects.logged[0]["book_id"] == "book_demo"
    assert defects.logged[0]["kind"] == "seam_repair"
    assert defects.logged[0]["shot_id"] == shot1_id
    assert defects.logged[0]["detail"] == {"action": "degrade"}
    # Every shot was actually re-rendered through the real Ken-Burns fallback —
    # the deliberately mismatched (600, 800) clip is gone, replaced by a
    # uniform FILM_SIZE clip.
    for shot_result in result.rendered:
        info = degrade.probe(shot_result.clip_bytes)
        assert (info.width, info.height) == degrade.FILM_SIZE


async def test_render_event_regens_shot_on_persistence_drift_alone() -> None:
    """detect_persistence_drift is wired as a SECOND repair gate: an
    unmotivated wardrobe drift across an otherwise-flawless chained seam (no
    geometry problem, no hard cut — canon-chained with a matching hand-off)
    must ALSO route through _repair, never weaker than what the geometry-only
    score already decided (here: ACCEPT)."""
    script = plan_event_script(
        event_id="evt_drift",
        book_id="book_demo",
        scene_id="scene_005",
        beats=_bridge_beats()[:2],
        # with_style=True: chained + handed off, geometry otherwise clean, AND
        # lighting pinned to a style token instead of each beat's own
        # (differing) mood, so wardrobe is this fixture's ONLY drift — see
        # _lighting_from's no-style fallback.
        canon=make_slice(with_style=True),
    )
    shot0, shot1 = script.shots[0], script.shots[1]
    shot0 = shot0.model_copy(
        update={"directive": shot0.directive.model_copy(update={"wardrobe": "a grey cloak"})}
    )
    shot1 = shot1.model_copy(
        update={"directive": shot1.directive.model_copy(update={"wardrobe": "a bright red gown"})}
    )
    script = script.model_copy(update={"shots": [shot0, shot1]})

    stills = {s.shot_id: png_bytes(720, 1280) for s in script.shots}
    renderer = _CountingKenBurnsRenderer()
    defects = FakeDefectRepo()
    director = EventDirector(renderer, defect_repo=defects)

    result = await director.render_event(script, stills=stills)

    assert result.shot_count == 2  # regen re-renders in place, never inserts
    assert renderer.calls.count(shot1.shot_id) == 2  # rendered, then regenerated
    assert renderer.calls.count(shot0.shot_id) == 1  # the earlier shot is untouched
    assert len(defects.logged) == 1
    assert defects.logged[0]["kind"] == "seam_repair"
    assert defects.logged[0]["shot_id"] == shot1.shot_id
    assert defects.logged[0]["detail"] == {"action": "regen_continuation"}


# --------------------------------------------------------------------------- #
# WS3 — lighting/setting persistence must be canon-grounded, never beat text
# --------------------------------------------------------------------------- #


def test_plan_event_script_lighting_and_setting_no_false_positive_drift() -> None:
    """A chained seam with NO canon style/location must NOT read as a
    lighting/setting drift just because consecutive beats' own mood/
    described_visuals text legitimately varies — that variation is the whole
    point of a beat, not an established canon fact. Regression test for the
    bug where ``_lighting_from``/``_setting_from`` fell back to
    ``beat.mood``/``beat.described_visuals`` (volatile per-beat free text)
    instead of ``None`` when canon had no style/location yet, which
    ``detect_persistence_drift`` then mistook for a real drift."""
    from app.render.continuity_qa import detect_persistence_drift

    beats = [
        Beat(
            beat_id="b0",
            scene_id="scene_009",
            beat_index=0,
            summary="A lone figure walks along the ridge, taking in the view before moving on.",
            mood="warm and golden",
            described_visuals="a sunlit meadow with golden light spilling over the hills",
            source_span=SourceSpan(page=1, word_range=(0, 20)),
        ),
        Beat(
            beat_id="b1",
            scene_id="scene_009",
            beat_index=1,
            summary=(
                "The figure continues down into the valley, following the worn path ahead."
            ),
            mood="cold and tense",
            described_visuals="a shadowed alley lined with cold grey stone",
            source_span=SourceSpan(page=1, word_range=(21, 40)),
        ),
    ]
    script = plan_event_script(
        event_id="evt_falsepos",
        book_id="book_demo",
        scene_id="scene_009",
        beats=beats,
        canon=make_slice(),  # a locked character (so shot 1 chains) but NO style, NO location
    )
    # Sanity: this really is a chained continuation seam (not a fresh, exempt
    # cut, and not a motivated reversal) — otherwise detect_persistence_drift
    # would trivially skip it regardless of the bug under test.
    assert script.shots[1].render_mode == RenderMode.VIDEO_CONTINUATION
    assert script.shots[1].directive.continues_from_shot_id == script.shots[0].shot_id
    assert script.shots[1].directive.motion_reversal is False

    persistence = detect_persistence_drift(script)

    assert persistence.ok is True
    assert persistence.drifts == ()


def test_plan_event_script_lighting_and_setting_drift_when_canon_grounded() -> None:
    """A GENUINE canon-grounded lighting/setting change across a chained seam
    — real style/location values that actually differ, exactly as
    ``_lighting_from``/``_setting_from`` read them from canon — must still be
    flagged as drift. Proves the false-positive fix only removed the
    beat-text fallback and did not weaken real detection."""
    from app.memory.interfaces import CanonEntitySlice
    from app.render.continuity_qa import detect_persistence_drift

    beat = Beat(
        beat_id="b0", scene_id="s", beat_index=0, summary="They cross into the next room."
    )
    canon_hall = make_slice().model_copy(
        update={
            "style": CanonEntitySlice(
                entity_key="style_main",
                type="style",
                name="Painterly storybook",
                version=1,
                style_tokens={"lighting": "warm firelight"},
                valid_from_beat=1,
            ),
            "location": CanonEntitySlice(
                entity_key="loc_hall",
                type="location",
                name="the great hall",
                version=1,
                description="a torch-lit banquet hall",
                valid_from_beat=1,
            ),
        }
    )
    assert canon_hall.style is not None and canon_hall.location is not None
    # canon_cellar varies only what this test needs to vary (the lighting token,
    # the location) — derived from canon_hall rather than restated, so the two
    # canon slices can't silently drift apart on the fields that don't matter.
    canon_cellar = canon_hall.model_copy(
        update={
            "style": canon_hall.style.model_copy(
                update={"style_tokens": {"lighting": "cold damp gloom"}}
            ),
            "location": canon_hall.location.model_copy(
                update={
                    "entity_key": "loc_cellar",
                    "name": "the flooded cellar",
                    "description": "a dripping stone cellar lit by a single lantern",
                    "valid_from_beat": 2,
                }
            ),
        }
    )
    # Both values are genuinely canon-grounded (never the beat's own mood /
    # described_visuals text, which is identical for both calls below) and
    # genuinely differ — the real-drift scenario the fix must keep catching.
    lighting_a, lighting_b = _lighting_from(canon_hall, beat), _lighting_from(canon_cellar, beat)
    setting_a, setting_b = _setting_from(canon_hall, beat), _setting_from(canon_cellar, beat)
    assert None not in (lighting_a, lighting_b, setting_a, setting_b)
    assert lighting_a != lighting_b
    assert setting_a != setting_b

    # plan_event_script applies a SINGLE canon slice to every shot in one call,
    # so shot0 (planned with canon_hall) already carries lighting_a/setting_a
    # exactly — only shot1 needs overriding to stand in for "canon moved on to
    # canon_cellar by the time this later, chained shot was planned".
    script = plan_event_script(
        event_id="evt_realdrift",
        book_id="book_demo",
        scene_id="s",
        beats=[beat, beat.model_copy(update={"beat_id": "b1", "beat_index": 1})],
        canon=canon_hall,
    )
    shot0, shot1 = script.shots
    assert (shot0.directive.lighting, shot0.directive.setting) == (lighting_a, setting_a)
    shot1 = shot1.model_copy(
        update={
            "directive": shot1.directive.model_copy(
                update={"lighting": lighting_b, "setting": setting_b}
            )
        }
    )
    script = script.model_copy(update={"shots": [shot0, shot1]})
    assert script.shots[1].render_mode == RenderMode.VIDEO_CONTINUATION  # still a chained seam
    assert script.shots[1].directive.continues_from_shot_id == script.shots[0].shot_id

    persistence = detect_persistence_drift(script)

    assert persistence.ok is False
    assert {d.dimension for d in persistence.drifts} == {"lighting", "setting"}
