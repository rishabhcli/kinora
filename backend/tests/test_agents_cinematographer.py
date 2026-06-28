"""Unit tests for the Cinematographer: the §9.3 render-mode decision tree (pure,
per branch) and shot design (verbatim ref selection, no invention). No network."""

from __future__ import annotations

import json

from app.agents.cinematographer import (
    Cinematographer,
    RenderModeInputs,
    build_brief,
    build_segment_brief,
    decide_render_mode,
    locked_reference_ids,
    style_override_from_notes,
)
from app.agents.contracts import Beat, DirectorNote, RenderMode, SourceSpan
from app.agents.prompts import SEGMENT
from app.memory.interfaces import CanonEntitySlice, CanonSlice, EndpointFrame, RefImage
from app.providers import Providers
from app.render.segment_packer import Segment
from tests.test_agents_support import (
    JsonSequencer,
    providers,  # noqa: F401  (pytest fixture)
)


def test_tree_first_last_frame() -> None:
    inputs = RenderModeInputs(
        locked_character_present=True, needs_motion=True, must_land_exact_pose=True
    )
    assert decide_render_mode(inputs) is RenderMode.FIRST_LAST_FRAME


def test_tree_video_continuation() -> None:
    inputs = RenderModeInputs(
        locked_character_present=True,
        needs_motion=True,
        must_land_exact_pose=False,
        prev_shot_accepted_continuous=True,
    )
    assert decide_render_mode(inputs) is RenderMode.VIDEO_CONTINUATION


def test_tree_reference_to_video() -> None:
    inputs = RenderModeInputs(locked_character_present=True, needs_motion=True)
    assert decide_render_mode(inputs) is RenderMode.REFERENCE_TO_VIDEO


def test_tree_text_to_video_for_establishing_no_character() -> None:
    inputs = RenderModeInputs(is_establishing_no_character=True)
    assert decide_render_mode(inputs) is RenderMode.TEXT_TO_VIDEO


def test_tree_instruction_edit() -> None:
    inputs = RenderModeInputs(
        locked_character_present=True,
        needs_motion=False,  # an edit of existing footage is not fresh motion
        is_establishing_no_character=False,
        minor_edit_on_accepted_clip=True,
    )
    assert decide_render_mode(inputs) is RenderMode.INSTRUCTION_EDIT


def test_tree_fallbacks() -> None:
    # Locked character but not motion / not establishing / not edit -> ref-to-video.
    assert decide_render_mode(RenderModeInputs(locked_character_present=True)) is (
        RenderMode.REFERENCE_TO_VIDEO
    )
    # Nothing known -> text-to-video.
    assert decide_render_mode(RenderModeInputs()) is RenderMode.TEXT_TO_VIDEO


def _slice_with_locked_hero() -> CanonSlice:
    hero = CanonEntitySlice(
        entity_key="char_hero",
        type="character",
        name="Hero",
        version=3,
        reference_images=[RefImage(key="refs/hero/front.png", locked=True)],
        valid_from_beat=1,
    )
    return CanonSlice(
        book_id="book_x",
        beat_id="beat_0001",
        beat_index=1,
        scene_id="scene_001",
        characters=[hero],
    )


def test_locked_reference_ids_are_versioned() -> None:
    assert locked_reference_ids(_slice_with_locked_hero()) == ["char_hero@v3"]


async def test_design_shot_uses_tree_mode_and_drops_invented_refs(providers: Providers) -> None:  # noqa: F811
    fill = {
        "prompt": "Hero stands at the gate, slow push-in, cool palette",
        "negative_prompt": "extra fingers, warped face",
        # Includes one VALID locked id and one INVENTED id (must be dropped).
        "reference_image_ids": ["char_hero@v3", "char_ghost@v9"],
        "camera": {"move": "push_in", "speed": "slow", "shot_size": "medium"},
        "seed": 88123,
    }
    providers.chat.chat_json = JsonSequencer(fill)  # type: ignore[method-assign]
    beat = Beat(beat_id="beat_0001", scene_id="scene_001", summary="hero at the gate")

    spec = await Cinematographer(providers).design_shot(beat, _slice_with_locked_hero())

    # Locked character + motion, no exact pose, no previous endpoint -> ref-to-video.
    assert spec.render_mode is RenderMode.REFERENCE_TO_VIDEO
    assert spec.reference_image_ids == ["char_hero@v3"]  # invented id refused
    assert spec.prompt.startswith("Hero stands")
    assert spec.camera.move == "push_in"
    assert spec.seed == 88123
    assert spec.shot_id == "beat_0001_shot_00"


async def test_design_shot_continuation_when_previous_endpoint(providers: Providers) -> None:  # noqa: F811
    canon = _slice_with_locked_hero()
    canon = canon.model_copy(
        update={"previous_endpoint": EndpointFrame(shot_id="prev", last_frame_key="lf.png")}
    )
    providers.chat.chat_json = JsonSequencer({"prompt": "p", "seed": 1})  # type: ignore[method-assign]
    beat = Beat(beat_id="beat_0002", scene_id="scene_001", summary="hero advances")

    spec = await Cinematographer(providers).design_shot(beat, canon)
    assert spec.render_mode is RenderMode.VIDEO_CONTINUATION


# --------------------------------------------------------------------------- #
# design_segment — one continuous ≤15s i2v take over a packed beat-run (overhaul)
# --------------------------------------------------------------------------- #


def _segment(duration_s: float = 12.5) -> Segment:
    return Segment(
        segment_id="scene_001_seg_00",
        ordinal=0,
        beat_ids=["beat_0001", "beat_0002"],
        source_span=SourceSpan(page=1, word_range=(0, 30)),
        duration_s=duration_s,
    )


def _segment_beats() -> list[Beat]:
    return [
        Beat(beat_id="beat_0001", scene_id="scene_001", summary="hero approaches the gate"),
        Beat(
            beat_id="beat_0002",
            scene_id="scene_001",
            summary="hero pushes it open and steps through",
        ),
    ]


async def test_design_segment_is_one_continuous_take(providers: Providers) -> None:  # noqa: F811
    """A packed segment designs ONE clip whose duration is the packed ≤15s length,
    keyed by the segment id, with invented refs dropped (verbatim locked only)."""
    fill = {
        "prompt": "Hero approaches the gate then pushes through; slow push from wide to medium",
        "negative_prompt": "extra fingers, warped face",
        "reference_image_ids": ["char_hero@v3", "char_ghost@v9"],  # one valid, one invented
        "camera": {"move": "push_in", "speed": "slow", "shot_size": "wide"},
        "seed": 4242,
    }
    providers.chat.chat_json = JsonSequencer(fill)  # type: ignore[method-assign]

    spec = await Cinematographer(providers).design_segment(
        _segment(12.5), _segment_beats(), _slice_with_locked_hero()
    )

    assert spec.shot_id == "scene_001_seg_00"
    assert spec.render_mode is RenderMode.REFERENCE_TO_VIDEO  # locked char, no anchor
    assert spec.target_duration_s == 12.5  # the packed segment duration, not a 5s shot
    assert spec.reference_image_ids == ["char_hero@v3"]  # invented id refused
    assert spec.prompt.startswith("Hero approaches")
    assert spec.seed == 4242


async def test_design_segment_uses_the_long_form_segment_prompt(providers: Providers) -> None:  # noqa: F811
    """The overhaul: design_segment drives the model with the segment@v1 prompt,
    not the per-shot cinematographer prompt."""
    captured: dict[str, str] = {}

    async def recording(messages, model, **kwargs):  # type: ignore[no-untyped-def]
        captured["system"] = messages[0]["content"]
        return {"prompt": "p", "seed": 1}

    providers.chat.chat_json = recording  # type: ignore[method-assign]
    await Cinematographer(providers).design_segment(
        _segment(), _segment_beats(), _slice_with_locked_hero()
    )
    assert captured["system"] == SEGMENT.system
    assert SEGMENT.version == "segment@v1"


async def test_design_segment_continues_from_previous_anchor(providers: Providers) -> None:  # noqa: F811
    """When the segment chains off the prior segment's last frame, the mode is a
    video continuation (anchored), not a fresh reference-to-video."""
    providers.chat.chat_json = JsonSequencer({"prompt": "p", "seed": 1})  # type: ignore[method-assign]
    spec = await Cinematographer(providers).design_segment(
        _segment(), _segment_beats(), _slice_with_locked_hero(), continues_from_previous=True
    )
    assert spec.render_mode is RenderMode.VIDEO_CONTINUATION


async def test_design_segment_text_to_video_when_no_character(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer({"prompt": "p", "seed": 1})  # type: ignore[method-assign]
    bare = CanonSlice(book_id="b", beat_id="beat_0001", beat_index=1, scene_id="scene_001")
    spec = await Cinematographer(providers).design_segment(_segment(), _segment_beats(), bare)
    assert spec.render_mode is RenderMode.TEXT_TO_VIDEO
    assert spec.reference_image_ids == []


# --------------------------------------------------------------------------- #
# Cinematic-language brief — the directorial eye + lens/lighting/grade
# --------------------------------------------------------------------------- #


def test_build_brief_picks_genre_profile_and_lens() -> None:
    """A scene's genre selects its directorial eye; the beat's cues pick the lens."""
    canon = _slice_with_locked_hero()
    intimate = Beat(
        beat_id="b", scene_id="scene_001", summary="a close look at her trembling hands"
    )
    brief = build_brief(intimate, canon)
    assert "85mm" in brief.lens  # intimate cue → portrait lens
    chase = Beat(
        beat_id="b", scene_id="scene_001", summary="a frantic chase, they sprint to escape"
    )
    assert build_brief(chase, canon).profile.name == "kinetic_action"


def test_build_brief_honours_canon_director_style_token() -> None:
    """A canon ``director_style`` style token overrides the genre default eye."""
    style = CanonEntitySlice(
        entity_key="style",
        type="style",
        name="Style",
        version=1,
        style_tokens={"director_style": "anamorphic_symmetry"},
        valid_from_beat=1,
    )
    canon = _slice_with_locked_hero().model_copy(update={"style": style})
    beat = Beat(beat_id="b", scene_id="scene_001", summary="a frantic chase")
    assert build_brief(beat, canon).profile.name == "anamorphic_symmetry"


def test_build_segment_brief_infers_over_all_beats() -> None:
    canon = _slice_with_locked_hero()
    beats = [
        Beat(beat_id="b0", scene_id="scene_001", summary="they share a tender embrace"),
        Beat(beat_id="b1", scene_id="scene_001", summary="lovers, full of longing"),
    ]
    assert build_segment_brief(beats, canon).profile.name == "romantic_soft"


async def test_design_shot_injects_cinematography_into_payload(providers: Providers) -> None:  # noqa: F811
    """The agent hands the model a ``cinematography`` block (the directorial eye)."""
    captured: dict[str, str] = {}

    async def recording(messages, model, **kwargs):  # type: ignore[no-untyped-def]
        captured["user"] = messages[1]["content"]
        return {"prompt": "p", "seed": 1}

    providers.chat.chat_json = recording  # type: ignore[method-assign]
    beat = Beat(beat_id="beat_0001", scene_id="scene_001", summary="a frantic chase, they sprint")
    await Cinematographer(providers).design_shot(beat, _slice_with_locked_hero())

    payload = json.loads(captured["user"])
    assert "cinematography" in payload
    assert payload["cinematography"]["director_style"] == "kinetic_action"
    assert payload["cinematography"]["genre"] == "action"
    assert "lens" in payload["cinematography"]
    assert "negative_floor" in payload["cinematography"]


async def test_design_shot_negative_floor_is_always_present(providers: Providers) -> None:  # noqa: F811
    """The deterministic negative floor is unioned into the spec; the model may add
    to it but never drop the universal artifacts or the genre's look-breakers."""
    fill = {"prompt": "p", "negative_prompt": "my custom artifact", "seed": 1}
    providers.chat.chat_json = JsonSequencer(fill)  # type: ignore[method-assign]
    beat = Beat(beat_id="beat_0001", scene_id="scene_001", summary="a tense, dangerous chase")
    spec = await Cinematographer(providers).design_shot(beat, _slice_with_locked_hero())

    assert spec.negative_prompt is not None
    assert "extra fingers" in spec.negative_prompt  # the universal floor survived
    assert "my custom artifact" in spec.negative_prompt  # the model's addition kept
    assert "motion smear" in spec.negative_prompt  # the action-genre look-breaker


def test_style_override_from_notes_picks_latest_named_look() -> None:
    notes = [
        DirectorNote(note="warmer please"),  # an axis ask → not a look
        DirectorNote(note="actually, shoot it like noir"),  # names a look
    ]
    assert style_override_from_notes(notes) == "noir_chiaroscuro"
    assert style_override_from_notes([DirectorNote(note="slower")]) is None


async def test_design_shot_style_note_reshoots_through_named_eye(providers: Providers) -> None:  # noqa: F811
    """A director note naming a look re-shoots the scene through that eye."""
    captured: dict[str, str] = {}

    async def recording(messages, model, **kwargs):  # type: ignore[no-untyped-def]
        captured["user"] = messages[1]["content"]
        return {"prompt": "p", "seed": 1}

    providers.chat.chat_json = recording  # type: ignore[method-assign]
    beat = Beat(beat_id="b", scene_id="s", summary="a frantic chase, they sprint")  # action genre
    await Cinematographer(providers).design_shot(
        beat, _slice_with_locked_hero(), [DirectorNote(note="shoot it like film noir")]
    )
    payload = json.loads(captured["user"])
    # The action default (kinetic_action) is overridden by the noir style note.
    assert payload["cinematography"]["director_style"] == "noir_chiaroscuro"
