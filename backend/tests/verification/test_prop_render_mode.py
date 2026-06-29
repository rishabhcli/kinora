"""Property tests for the §9.3 Wan-mode decision tree (``decide_render_mode``).

The function is a pure map from six booleans to a :class:`RenderMode`. With only
64 inputs the space is *finite*, so these properties amount to an exhaustive proof
of the documented tree plus the safety invariants the rest of the pipeline relies
on (e.g. text-to-video is the only mode with no character to lock).
"""

from __future__ import annotations

import itertools

from hypothesis import given

from app.agents.cinematographer import RenderModeInputs, decide_render_mode
from app.agents.contracts import RenderMode
from app.verification.properties.strategies import render_mode_inputs

ALL_INPUTS = [
    RenderModeInputs(*bits) for bits in itertools.product([False, True], repeat=6)
]


def test_total_and_deterministic_over_entire_space() -> None:
    """The tree is total (returns a valid mode for all 64 inputs) and deterministic."""
    for inp in ALL_INPUTS:
        first = decide_render_mode(inp)
        assert isinstance(first, RenderMode)
        # Determinism: same input → same output, every call.
        assert decide_render_mode(inp) is first


@given(render_mode_inputs)
def test_result_is_always_a_valid_mode(inp: RenderModeInputs) -> None:
    assert decide_render_mode(inp) in set(RenderMode)


@given(render_mode_inputs)
def test_locked_character_plus_motion_never_yields_text_to_video(
    inp: RenderModeInputs,
) -> None:
    """A locked character that needs motion must be pinned — never raw t2v (§9.3).

    Text-to-video has no reference to lock the appearance to, so routing a locked
    character there would be the exact face-drift bug the tree exists to prevent.
    """
    if inp.locked_character_present and inp.needs_motion:
        assert decide_render_mode(inp) is not RenderMode.TEXT_TO_VIDEO


@given(render_mode_inputs)
def test_exact_pose_dominates_when_locked_and_moving(inp: RenderModeInputs) -> None:
    """`must_land_exact_pose` wins the locked+motion branch → FLF (§9.3)."""
    if inp.locked_character_present and inp.needs_motion and inp.must_land_exact_pose:
        assert decide_render_mode(inp) is RenderMode.FIRST_LAST_FRAME


@given(render_mode_inputs)
def test_continuation_only_when_not_landing_a_pose(inp: RenderModeInputs) -> None:
    """Video-continuation requires locked+motion, a prior endpoint, and no pose-land."""
    mode = decide_render_mode(inp)
    if mode is RenderMode.VIDEO_CONTINUATION:
        assert inp.locked_character_present
        assert inp.needs_motion
        assert inp.prev_shot_accepted_continuous
        assert not inp.must_land_exact_pose


@given(render_mode_inputs)
def test_pose_strictly_precedes_continuation(inp: RenderModeInputs) -> None:
    """When both pose-land and prev-continuous hold, pose wins (branch order)."""
    if (
        inp.locked_character_present
        and inp.needs_motion
        and inp.must_land_exact_pose
        and inp.prev_shot_accepted_continuous
    ):
        assert decide_render_mode(inp) is RenderMode.FIRST_LAST_FRAME


@given(render_mode_inputs)
def test_text_to_video_implies_no_locked_character_in_motion(
    inp: RenderModeInputs,
) -> None:
    """If the tree picks t2v, there was no locked-character-in-motion to pin."""
    if decide_render_mode(inp) is RenderMode.TEXT_TO_VIDEO:
        assert not (inp.locked_character_present and inp.needs_motion)


@given(render_mode_inputs)
def test_instruction_edit_requires_minor_edit_flag(inp: RenderModeInputs) -> None:
    """INSTRUCTION_EDIT is reachable only via the minor-edit branch.

    (And only when there's no locked-character-in-motion ahead of it, since that
    branch is evaluated first.)
    """
    if decide_render_mode(inp) is RenderMode.INSTRUCTION_EDIT:
        assert inp.minor_edit_on_accepted_clip
        assert not (inp.locked_character_present and inp.needs_motion)


def test_reference_to_video_is_the_locked_fallback() -> None:
    """A locked character with no motion falls back to reference-to-video."""
    inp = RenderModeInputs(locked_character_present=True, needs_motion=False)
    assert decide_render_mode(inp) is RenderMode.REFERENCE_TO_VIDEO


def test_no_signals_falls_back_to_text_to_video() -> None:
    """The empty input (no character, no motion, no edit) → t2v fallback."""
    assert decide_render_mode(RenderModeInputs()) is RenderMode.TEXT_TO_VIDEO


def test_every_mode_except_image_to_video_is_reachable() -> None:
    """Exhaustive reachability: the tree can emit each documented mode.

    IMAGE_TO_VIDEO exists in the enum (provider parity) but the §9.3 *tree* never
    selects it — this test pins that fact so a future edit that silently starts
    emitting it (or stops emitting a documented mode) is caught.
    """
    produced = {decide_render_mode(inp) for inp in ALL_INPUTS}
    expected = {
        RenderMode.TEXT_TO_VIDEO,
        RenderMode.REFERENCE_TO_VIDEO,
        RenderMode.FIRST_LAST_FRAME,
        RenderMode.VIDEO_CONTINUATION,
        RenderMode.INSTRUCTION_EDIT,
    }
    assert produced == expected
    assert RenderMode.IMAGE_TO_VIDEO not in produced
