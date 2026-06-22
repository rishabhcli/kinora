"""Unit tests for the Cinematographer: the §9.3 render-mode decision tree (pure,
per branch) and shot design (verbatim ref selection, no invention). No network."""

from __future__ import annotations

from app.agents.cinematographer import (
    Cinematographer,
    RenderModeInputs,
    decide_render_mode,
    locked_reference_ids,
)
from app.agents.contracts import Beat, RenderMode
from app.memory.interfaces import CanonEntitySlice, CanonSlice, EndpointFrame, RefImage
from app.providers import Providers
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
