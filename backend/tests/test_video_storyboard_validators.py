"""Unit tests for storyboard validators catching planted defects (no network)."""

from __future__ import annotations

from app.agents.contracts import SourceSpan
from app.video.storyboard import (
    ContinuityKind,
    ContinuityLink,
    PassageBeat,
    ShotCoverage,
    ShotIntentShape,
    Storyboard,
    StoryboardBudget,
    StoryboardShot,
    has_errors,
    validate_storyboard,
)


def _beat(beat_id: str, rng: tuple[int, int], entities: list[str]) -> PassageBeat:
    return PassageBeat(
        beat_id=beat_id,
        text="x " * (rng[1] - rng[0]),
        word_range=rng,
        entities=entities,
    )


def _shot(
    shot_id: str,
    beat_id: str,
    rng: tuple[int, int],
    *,
    narration: str = "some narration",
    entities: list[str] | None = None,
    continuity: ContinuityLink | None = None,
    duration: float = 5.0,
) -> StoryboardShot:
    return StoryboardShot(
        shot_id=shot_id,
        beat_id=beat_id,
        duration_s=duration,
        coverage=ShotCoverage.MASTER,
        entities=entities or [],
        source_span=SourceSpan(word_range=rng),
        narration=narration,
        continuity=continuity or ContinuityLink(kind=ContinuityKind.SCENE_START),
        intent=ShotIntentShape(reference_entities=entities or []),
    )


def _valid_storyboard() -> tuple[Storyboard, list[PassageBeat]]:
    beats = [_beat("b0", (0, 30), ["mara"]), _beat("b1", (30, 60), ["mara"])]
    shots = [
        _shot("s0", "b0", (0, 30), entities=["mara"], duration=5.0),
        _shot(
            "s1",
            "b1",
            (30, 60),
            entities=["mara"],
            duration=5.0,
            continuity=ContinuityLink(kind=ContinuityKind.HARD_CUT, from_shot_id="s0"),
        ),
    ]
    sb = Storyboard(
        passage_id="p",
        shots=shots,
        budget=StoryboardBudget(target_total_s=10.0, tolerance_s=2.0, max_shots=12),
    )
    return sb, beats


def test_valid_storyboard_has_no_errors() -> None:
    sb, beats = _valid_storyboard()
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    assert not has_errors(issues), [i.code for i in issues]


def test_orphan_entity_not_in_canon_is_caught() -> None:
    sb, beats = _valid_storyboard()
    sb.shots[0].entities.append("dragon")  # never in canon context
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "orphan_entity_not_in_canon" in codes


def test_orphan_entity_not_in_beat_is_caught() -> None:
    # In canon, but the parent beat never named it.
    sb, beats = _valid_storyboard()
    sb.shots[0].entities.append("tomas")
    issues = validate_storyboard(sb, beats, allowed_entities={"mara", "tomas"})
    codes = {i.code for i in issues}
    assert "orphan_entity_not_in_beat" in codes


def test_duration_out_of_band_is_caught() -> None:
    sb, beats = _valid_storyboard()
    sb.shots[0].duration_s = 20.0  # above the 8s ceiling
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "shot_duration_out_of_band" in codes


def test_total_duration_off_budget_is_caught() -> None:
    sb, beats = _valid_storyboard()
    # Target 10 ± 2, but the two 8s shots sum to 16 → 6s drift.
    sb.shots[0].duration_s = 8.0
    sb.shots[1].duration_s = 8.0
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "total_duration_off_budget" in codes


def test_shot_count_over_budget_is_caught() -> None:
    sb, beats = _valid_storyboard()
    sb.budget = sb.budget.model_copy(update={"max_shots": 1})
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "shot_count_over_budget" in codes


def test_missing_narration_is_caught() -> None:
    sb, beats = _valid_storyboard()
    sb.shots[1].narration = "   "
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "shot_missing_narration" in codes


def test_uncovered_beat_is_caught() -> None:
    beats = [_beat("b0", (0, 30), ["mara"]), _beat("b1", (30, 60), ["mara"])]
    # Only b0 has a shot; b1 is orphaned.
    sb = Storyboard(
        passage_id="p",
        shots=[_shot("s0", "b0", (0, 30), entities=["mara"])],
        budget=StoryboardBudget(target_total_s=5.0, tolerance_s=2.0),
    )
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "beat_uncovered" in codes


def test_coverage_gap_inside_beat_is_caught() -> None:
    beats = [_beat("b0", (0, 60), ["mara"])]
    # Two shots covering (0,20) and (40,60) — a gap at (20,40).
    shots = [
        _shot("s0", "b0", (0, 20), entities=["mara"], duration=4.0),
        _shot(
            "s1",
            "b0",
            (40, 60),
            entities=["mara"],
            duration=4.0,
            continuity=ContinuityLink(kind=ContinuityKind.HARD_CUT, from_shot_id="s0"),
        ),
    ]
    sb = Storyboard(
        passage_id="p", shots=shots, budget=StoryboardBudget(target_total_s=8.0, tolerance_s=2.0)
    )
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "beat_coverage_gap" in codes


def test_mid_storyboard_scene_start_is_caught() -> None:
    sb, beats = _valid_storyboard()
    sb.shots[1].continuity = ContinuityLink(kind=ContinuityKind.SCENE_START)
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "mid_shot_scene_start" in codes


def test_continuity_anchor_mismatch_is_caught() -> None:
    sb, beats = _valid_storyboard()
    sb.shots[1].continuity = ContinuityLink(
        kind=ContinuityKind.HARD_CUT, from_shot_id="s_wrong"
    )
    issues = validate_storyboard(sb, beats, allowed_entities={"mara"})
    codes = {i.code for i in issues}
    assert "continuity_anchor_not_predecessor" in codes
