"""Unit tests for the storyboard typed models + budget value object (no network)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.video.storyboard import (
    CanonContext,
    ContinuityKind,
    ContinuityLink,
    ShotCoverage,
    Storyboard,
    StoryboardBudget,
    StoryboardShot,
)


def test_canon_context_is_locked() -> None:
    ctx = CanonContext(entities=["a", "b"], locked_entities=["a"])
    assert ctx.is_locked("a") is True
    assert ctx.is_locked("b") is False
    assert ctx.is_locked("missing") is False


def test_budget_rejects_nonpositive_target() -> None:
    with pytest.raises(ValidationError):
        StoryboardBudget(target_total_s=0)


def test_budget_rejects_inverted_band() -> None:
    with pytest.raises(ValidationError):
        StoryboardBudget(min_shot_s=8.0, max_shot_s=3.0)


def test_budget_rejects_inverted_shot_bounds() -> None:
    with pytest.raises(ValidationError):
        StoryboardBudget(min_shots=5, max_shots=2)


def test_budget_from_settings_uses_attrs_and_defaults() -> None:
    class _Stub:
        storyboard_target_total_s = 45.0
        storyboard_max_shots = 7
        # tolerance / band intentionally omitted → defaults

    b = StoryboardBudget.from_settings(_Stub())
    assert b.target_total_s == 45.0
    assert b.max_shots == 7
    assert b.min_shot_s == 3.0  # default
    assert b.max_shot_s == 8.0  # default


def test_storyboard_totals_are_derived() -> None:
    sb = Storyboard(
        passage_id="p",
        shots=[
            StoryboardShot(shot_id="s0", beat_id="b0", duration_s=5.0),
            StoryboardShot(shot_id="s1", beat_id="b0", duration_s=3.5),
        ],
    )
    assert sb.shot_count == 2
    assert sb.total_duration_s == 8.5


def test_continuity_link_defaults_to_scene_start() -> None:
    link = ContinuityLink()
    assert link.kind is ContinuityKind.SCENE_START
    assert link.from_shot_id is None
    assert link.shares_first_frame is False


def test_character_coverage_membership() -> None:
    from app.video.storyboard.models import CHARACTER_COVERAGE

    assert ShotCoverage.MASTER in CHARACTER_COVERAGE
    assert ShotCoverage.ESTABLISHING not in CHARACTER_COVERAGE
