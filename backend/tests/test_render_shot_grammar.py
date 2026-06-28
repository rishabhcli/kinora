"""Shot grammar (Agent 1, WS3) — establishing→medium→insert, screen-direction, 180°.

Deterministic film grammar across an event: the first shot establishes wide, a
pose-landing beat tightens to a close insert, and a character's screen direction
is held consistent (the 180° rule) — only flipping when the text motivates a
reversal ("turns to face"). All pure, so the grammar is exhaustively testable.
"""

from __future__ import annotations

from app.agents.contracts import Beat
from app.render.shot_grammar import (
    AxisViolation,
    ScreenDirection,
    detect_axis_violations,
    eyeline_consistent,
    is_motion_reversal,
    resolve_screen_directions,
    screen_direction_for_beat,
    shot_size_for,
    shot_sizes_for_event,
    violates_180,
)
from tests.test_render_event_director import _bridge_beats


def _beat(summary: str, mood: str = "") -> Beat:
    return Beat(beat_id="b", scene_id="s", beat_index=0, summary=summary, mood=mood)


def test_screen_direction_detected_from_motion_cues() -> None:
    assert screen_direction_for_beat(_beat("She runs to the right along the wall")) == (
        ScreenDirection.LEFT_TO_RIGHT
    )
    assert screen_direction_for_beat(_beat("He edges leftward into the alley")) == (
        ScreenDirection.RIGHT_TO_LEFT
    )
    assert screen_direction_for_beat(_beat("The ship recedes into the distance")) == (
        ScreenDirection.AWAY
    )
    assert screen_direction_for_beat(_beat("A still, empty room.")) == ScreenDirection.NEUTRAL


def test_resolve_carries_direction_forward_and_flips_only_on_reversal() -> None:
    beats = [
        _beat("She sprints to the right across the bridge."),  # establishes L2R
        _beat("The planks blur beneath her boots."),  # no cue → hold L2R
        _beat("She turns to face the pursuers."),  # reversal → flip to R2L
        _beat("She steadies her breath, waiting."),  # no directional cue → hold R2L
    ]
    dirs = resolve_screen_directions(beats)
    assert dirs == [
        ScreenDirection.LEFT_TO_RIGHT,
        ScreenDirection.LEFT_TO_RIGHT,
        ScreenDirection.RIGHT_TO_LEFT,
        ScreenDirection.RIGHT_TO_LEFT,
    ]


def test_bridge_event_directions_and_reversal_flag() -> None:
    beats = _bridge_beats()
    dirs = resolve_screen_directions(beats)
    # b0 has no motion cue (neutral establishing), b1 sprints across (L2R), b2
    # "turns to face" is a motivated reversal (R2L) — not a 180° error.
    assert dirs[1] == ScreenDirection.LEFT_TO_RIGHT
    assert dirs[2] == ScreenDirection.RIGHT_TO_LEFT
    assert is_motion_reversal(beats[2]) is True
    assert is_motion_reversal(beats[1]) is False


def test_shot_sizes_progress_establishing_then_insert() -> None:
    beats = _bridge_beats()
    sizes = shot_sizes_for_event(beats)
    assert sizes[0] == "wide"  # establishing
    assert sizes[-1] == "close"  # "turns to face" pose → close insert
    assert len(set(sizes)) > 1  # genuine grammar, not one size repeated


def test_shot_size_for_close_on_intimate_cue() -> None:
    assert shot_size_for(2, _beat("A close look at her trembling hands.")) == "close"
    assert shot_size_for(0, _beat("Anything at all")) == "wide"  # ordinal 0 establishes


def test_violates_180_only_on_unmotivated_flip() -> None:
    assert (
        violates_180(ScreenDirection.LEFT_TO_RIGHT, ScreenDirection.RIGHT_TO_LEFT, reversal=False)
        is True
    )
    # The same flip, but the text motivated the reversal → not a 180° violation.
    assert (
        violates_180(ScreenDirection.LEFT_TO_RIGHT, ScreenDirection.RIGHT_TO_LEFT, reversal=True)
        is False
    )
    # Holding direction, or a neutral shot, never violates the line.
    assert (
        violates_180(ScreenDirection.LEFT_TO_RIGHT, ScreenDirection.LEFT_TO_RIGHT, reversal=False)
        is False
    )
    assert (
        violates_180(ScreenDirection.NEUTRAL, ScreenDirection.RIGHT_TO_LEFT, reversal=False)
        is False
    )


# --------------------------------------------------------------------------- #
# Axis tracking + eyeline consistency
# --------------------------------------------------------------------------- #


def test_detect_axis_violations_flags_unmotivated_flip() -> None:
    beats = [
        _beat("She sprints to the right across the bridge."),  # L2R
        _beat("She edges leftward without turning."),  # flip to R2L, NOT motivated
    ]
    violations = detect_axis_violations(beats)
    assert len(violations) == 1
    v = violations[0]
    assert isinstance(v, AxisViolation)
    assert v.ordinal == 1
    assert v.prev is ScreenDirection.LEFT_TO_RIGHT
    assert v.cur is ScreenDirection.RIGHT_TO_LEFT


def test_detect_axis_violations_clean_on_motivated_reversal() -> None:
    beats = [
        _beat("She sprints to the right across the bridge."),  # L2R
        _beat("She turns to face the pursuers."),  # motivated reversal → clean
    ]
    assert detect_axis_violations(beats) == []


def test_detect_axis_violations_clean_when_line_held() -> None:
    # The bridge event holds its line (the one flip is a motivated "turns to face").
    assert detect_axis_violations(_bridge_beats()) == []


def test_eyeline_consistent_for_opposed_gazes() -> None:
    # A correctly-blocked two-hander: A looks right, B looks left → they meet.
    assert (
        eyeline_consistent(ScreenDirection.LEFT_TO_RIGHT, ScreenDirection.RIGHT_TO_LEFT) is True
    )
    # Both looking the same way is a broken eyeline.
    assert (
        eyeline_consistent(ScreenDirection.LEFT_TO_RIGHT, ScreenDirection.LEFT_TO_RIGHT) is False
    )
    # A head-on (neutral) single never breaks the match.
    assert eyeline_consistent(ScreenDirection.NEUTRAL, ScreenDirection.LEFT_TO_RIGHT) is True
