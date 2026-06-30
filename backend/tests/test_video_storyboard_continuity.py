"""Unit tests for continuity hand-offs between consecutive shots (§9.3)."""

from __future__ import annotations

from app.agents.contracts import RenderMode
from app.video.storyboard import (
    ContinuityKind,
    ShotCoverage,
    ShotIntentShape,
    StoryboardShot,
    link_continuity,
)


def _shot(
    shot_id: str,
    beat_id: str,
    coverage: ShotCoverage,
    *,
    entities: list[str] | None = None,
    refs: list[str] | None = None,
    mode: RenderMode = RenderMode.REFERENCE_TO_VIDEO,
) -> StoryboardShot:
    ents = entities or []
    return StoryboardShot(
        shot_id=shot_id,
        beat_id=beat_id,
        coverage=coverage,
        render_mode=mode,
        entities=ents,
        intent=ShotIntentShape(reference_entities=refs if refs is not None else ents),
    )


def test_first_shot_is_scene_start() -> None:
    shots = link_continuity([_shot("s0", "b0", ShotCoverage.MASTER, entities=["mara"])])
    assert shots[0].continuity.kind is ContinuityKind.SCENE_START
    assert shots[0].continuity.from_shot_id is None


def test_same_beat_same_character_is_continuous() -> None:
    shots = link_continuity(
        [
            _shot("s0", "b0", ShotCoverage.MASTER, entities=["mara"]),
            _shot("s1", "b0", ShotCoverage.REACTION, entities=["mara"]),
        ]
    )
    link = shots[1].continuity
    assert link.kind is ContinuityKind.CONTINUOUS
    assert link.from_shot_id == "s0"
    assert link.shares_first_frame is True
    # Continuous take upgrades the render mode.
    assert shots[1].render_mode is RenderMode.VIDEO_CONTINUATION


def test_cross_beat_match_frame_uses_flf() -> None:
    # Different beats, shared character, master → insert reads as a graphic match.
    shots = link_continuity(
        [
            _shot("s0", "b0", ShotCoverage.MASTER, entities=["mara"]),
            _shot("s1", "b1", ShotCoverage.INSERT, entities=["mara"]),
        ]
    )
    link = shots[1].continuity
    assert link.kind is ContinuityKind.MATCH_FRAME
    assert shots[1].render_mode is RenderMode.FIRST_LAST_FRAME
    assert link.shares_first_frame is True


def test_no_shared_character_is_hard_cut() -> None:
    shots = link_continuity(
        [
            _shot("s0", "b0", ShotCoverage.MASTER, entities=["mara"]),
            _shot("s1", "b1", ShotCoverage.MASTER, entities=["tomas"]),
        ]
    )
    assert shots[1].continuity.kind is ContinuityKind.HARD_CUT
    # Unchanged render mode (no upgrade without a shared anchor).
    assert shots[1].render_mode is RenderMode.REFERENCE_TO_VIDEO


def test_establishing_wide_is_never_made_continuous() -> None:
    # An establishing wide has no entities to anchor on → hard cut, t2v stays.
    shots = link_continuity(
        [
            _shot(
                "s0",
                "b0",
                ShotCoverage.ESTABLISHING,
                entities=[],
                mode=RenderMode.TEXT_TO_VIDEO,
            ),
            _shot("s1", "b0", ShotCoverage.MASTER, entities=["mara"]),
        ]
    )
    assert shots[1].continuity.kind is ContinuityKind.HARD_CUT


def test_anchor_always_points_at_predecessor() -> None:
    shots = link_continuity(
        [
            _shot("s0", "b0", ShotCoverage.MASTER, entities=["mara"]),
            _shot("s1", "b0", ShotCoverage.REACTION, entities=["mara"]),
            _shot("s2", "b1", ShotCoverage.MASTER, entities=["mara"]),
        ]
    )
    for prev, nxt in zip(shots, shots[1:], strict=False):
        if nxt.continuity.from_shot_id is not None:
            assert nxt.continuity.from_shot_id == prev.shot_id
