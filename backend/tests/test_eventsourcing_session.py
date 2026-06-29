"""Exhaustive decision-function tests for the reading-Session aggregate
(§5.2-§5.4, §9.6). Pure: build from events, decide, assert emitted events."""

from __future__ import annotations

import pytest

from app.db.models.enums import SessionMode
from app.eventsourcing.domain.errors import CommandRejected, InvariantViolation, ValidationError
from app.eventsourcing.domain.session import (
    DirectorCommentLeft,
    IntentUpdated,
    ModeSwitched,
    PreferenceRecorded,
    SessionAggregate,
    SessionEnded,
    SessionStarted,
)


def _started() -> SessionAggregate:
    agg = SessionAggregate("s1")
    agg.start(user_id="u1", book_id="b1")
    agg.mark_committed()
    return agg


def test_start_emits_session_started() -> None:
    agg = SessionAggregate("s1")
    agg.start(user_id="u1", book_id="b1")
    (event,) = agg.uncommitted
    assert isinstance(event, SessionStarted)
    assert event.user_id == "u1"
    assert agg.started is True
    assert agg.mode is SessionMode.VIEWER


def test_start_requires_user_and_book() -> None:
    with pytest.raises(ValidationError):
        SessionAggregate("s1").start(user_id="", book_id="b1")


def test_cannot_start_twice() -> None:
    agg = _started()
    with pytest.raises(CommandRejected, match="already started"):
        agg.start(user_id="u1", book_id="b1")


def test_commands_rejected_before_start() -> None:
    agg = SessionAggregate("s1")
    with pytest.raises(CommandRejected, match="not started"):
        agg.update_intent(focus_word=10, velocity=1.0)


def test_intent_update_emits_on_material_change() -> None:
    agg = _started()
    emitted = agg.update_intent(focus_word=100, velocity=2.0)
    assert emitted is True
    (event,) = agg.uncommitted
    assert isinstance(event, IntentUpdated)
    assert event.focus_word == 100


def test_intent_update_absorbs_sub_epsilon_nudge() -> None:
    agg = _started()
    agg.update_intent(focus_word=100, velocity=2.0)
    agg.mark_committed()
    # A tiny nudge below both epsilons emits nothing.
    emitted = agg.update_intent(focus_word=101, velocity=2.01)
    assert emitted is False
    assert agg.uncommitted == ()


def test_intent_rejects_negative_focus() -> None:
    agg = _started()
    with pytest.raises(ValidationError):
        agg.update_intent(focus_word=-1, velocity=0.0)


def test_switch_mode_toggles_and_is_idempotent() -> None:
    agg = _started()
    assert agg.switch_mode(mode=SessionMode.DIRECTOR) is True
    (event,) = agg.uncommitted
    assert isinstance(event, ModeSwitched)
    assert agg.mode is SessionMode.DIRECTOR
    agg.mark_committed()
    # Switching to the same mode is a no-op.
    assert agg.switch_mode(mode=SessionMode.DIRECTOR) is False
    assert agg.uncommitted == ()


def test_comment_requires_director_mode() -> None:
    agg = _started()  # in Viewer
    with pytest.raises(InvariantViolation, match="Director mode"):
        agg.leave_comment(
            comment_id="c1", shot_id="shot1", note="x", routed_agent="cinematographer"
        )


def test_comment_in_director_mode_emits() -> None:
    agg = _started()
    agg.switch_mode(mode=SessionMode.DIRECTOR)
    agg.mark_committed()
    agg.leave_comment(
        comment_id="c1",
        shot_id="shot1",
        note="make her coat red",
        routed_agent="cinematographer",
        region=(0.1, 0.2, 0.3, 0.4),
    )
    (event,) = agg.uncommitted
    assert isinstance(event, DirectorCommentLeft)
    assert event.shot_id == "shot1"
    assert event.region == (0.1, 0.2, 0.3, 0.4)
    assert agg.comment_count == 1


def test_comment_rejects_empty_note() -> None:
    agg = _started()
    agg.switch_mode(mode=SessionMode.DIRECTOR)
    with pytest.raises(ValidationError):
        agg.leave_comment(comment_id="c1", shot_id="shot1", note="   ", routed_agent="x")


def test_record_preference_dedupes_unchanged() -> None:
    agg = _started()
    assert agg.record_preference(key="pacing", value="slow") is True
    (event,) = agg.uncommitted
    assert isinstance(event, PreferenceRecorded)
    agg.mark_committed()
    assert agg.record_preference(key="pacing", value="slow") is False
    assert agg.preferences == {"pacing": "slow"}


def test_end_is_idempotent() -> None:
    agg = _started()
    assert agg.end(reason="idle") is True
    (event,) = agg.uncommitted
    assert isinstance(event, SessionEnded)
    assert event.reason == "idle"
    agg.mark_committed()
    assert agg.end() is False  # already ended


def test_cannot_end_unstarted() -> None:
    with pytest.raises(CommandRejected, match="never started"):
        SessionAggregate("s1").end()


def test_no_commands_after_end() -> None:
    agg = _started()
    agg.end()
    agg.mark_committed()
    with pytest.raises(CommandRejected, match="has ended"):
        agg.update_intent(focus_word=5, velocity=1.0)


def test_replay_reconstructs_full_state() -> None:
    agg = SessionAggregate("s1")
    agg.replay(
        [
            SessionStarted(session_id="s1", user_id="u1", book_id="b1"),
            ModeSwitched(session_id="s1", mode=SessionMode.DIRECTOR.value),
            DirectorCommentLeft(
                session_id="s1", comment_id="c1", shot_id="shot1", note="n", routed_agent="a"
            ),
            PreferenceRecorded(session_id="s1", user_id="u1", key="palette", value="warm"),
            SessionEnded(session_id="s1", reason="closed"),
        ]
    )
    assert agg.started and agg.ended
    assert agg.mode is SessionMode.DIRECTOR
    assert agg.comment_count == 1
    assert agg.preferences == {"palette": "warm"}
    assert agg.version == 5
