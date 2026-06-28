"""Unit tests for the review state machine (pure transition graph)."""

from __future__ import annotations

import pytest

from app.moderation.contracts import ReviewState
from app.moderation.review import (
    TRANSITIONS,
    can_transition,
    content_down,
    content_reinstated,
    is_terminal,
)


def test_happy_path_transitions_are_legal() -> None:
    assert can_transition(ReviewState.PENDING, ReviewState.UNDER_REVIEW)
    assert can_transition(ReviewState.UNDER_REVIEW, ReviewState.APPROVED)
    assert can_transition(ReviewState.UNDER_REVIEW, ReviewState.REJECTED)
    assert can_transition(ReviewState.REJECTED, ReviewState.APPEALED)
    assert can_transition(ReviewState.APPEALED, ReviewState.APPEAL_GRANTED)
    assert can_transition(ReviewState.APPEALED, ReviewState.APPEAL_DENIED)


def test_takedown_and_escalation_paths() -> None:
    assert can_transition(ReviewState.PENDING, ReviewState.TAKEDOWN)
    assert can_transition(ReviewState.PENDING, ReviewState.ESCALATED)
    assert can_transition(ReviewState.ESCALATED, ReviewState.UNDER_REVIEW)
    assert can_transition(ReviewState.TAKEDOWN, ReviewState.APPEALED)


def test_illegal_transitions_are_rejected() -> None:
    # Can't approve a pending item without claiming it first.
    assert not can_transition(ReviewState.PENDING, ReviewState.APPROVED)
    # Can't un-approve a terminal state.
    assert not can_transition(ReviewState.APPROVED, ReviewState.PENDING)
    # Can't appeal something that was approved.
    assert not can_transition(ReviewState.APPROVED, ReviewState.APPEALED)
    # Can't jump straight from pending to appeal.
    assert not can_transition(ReviewState.PENDING, ReviewState.APPEALED)


@pytest.mark.parametrize(
    "state",
    [ReviewState.APPROVED, ReviewState.APPEAL_GRANTED, ReviewState.APPEAL_DENIED],
)
def test_terminal_states_have_no_exits(state: ReviewState) -> None:
    assert is_terminal(state)
    assert TRANSITIONS[state] == frozenset()


def test_content_visibility_predicates() -> None:
    assert content_reinstated(ReviewState.APPROVED)
    assert content_reinstated(ReviewState.APPEAL_GRANTED)
    assert not content_reinstated(ReviewState.REJECTED)

    assert content_down(ReviewState.REJECTED)
    assert content_down(ReviewState.TAKEDOWN)
    assert content_down(ReviewState.APPEAL_DENIED)
    assert not content_down(ReviewState.APPROVED)


def test_every_state_appears_in_graph() -> None:
    for state in ReviewState:
        assert state in TRANSITIONS, f"state {state} missing from transition graph"


def test_graph_targets_are_valid_states() -> None:
    for targets in TRANSITIONS.values():
        for target in targets:
            assert isinstance(target, ReviewState)
