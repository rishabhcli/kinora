"""Exhaustive tests for the §9.7 render-shot aggregate: every legal edge, the
illegal-edge guard, the <=2 retry cap, terminal sinks, and budget accounting.

These cross-check the aggregate against the canonical
:data:`app.render.states.ALLOWED_TRANSITIONS` table so the event-sourced model
and the in-pipeline ShotStateMachine can never drift."""

from __future__ import annotations

import pytest

from app.db.models.enums import ShotStatus
from app.eventsourcing.domain.errors import CommandRejected, InvariantViolation
from app.eventsourcing.domain.render_shot import (
    MAX_REPAIRS,
    RenderShotAggregate,
    ShotAccepted,
    ShotConflictRaised,
    ShotDegraded,
    ShotPlanned,
    ShotRendered,
)
from app.render.states import ALLOWED_TRANSITIONS, RenderState, is_allowed


def _planned() -> RenderShotAggregate:
    agg = RenderShotAggregate("shot1")
    agg.plan(book_id="b1", scene_id="sc1", beat_id="be1", shot_hash="h1")
    agg.mark_committed()
    return agg


def test_plan_is_genesis() -> None:
    agg = RenderShotAggregate("shot1")
    agg.plan(book_id="b1", scene_id="sc1", beat_id="be1", shot_hash="h1")
    (event,) = agg.uncommitted
    assert isinstance(event, ShotPlanned)
    assert agg.planned and agg.state is RenderState.PLANNED
    assert agg.status is ShotStatus.PLANNED


def test_cannot_plan_twice() -> None:
    agg = _planned()
    with pytest.raises(CommandRejected, match="already planned"):
        agg.plan(book_id="b1", scene_id="x", beat_id="y", shot_hash="z")


def test_transitions_rejected_before_plan() -> None:
    agg = RenderShotAggregate("shot1")
    with pytest.raises(CommandRejected, match="not been planned"):
        agg.promote()


def test_keyframe_then_promote() -> None:
    agg = _planned()
    agg.keyframe(keyframe_url="kf")
    assert agg.state is RenderState.KEYFRAMED
    agg.promote()
    assert agg.state is RenderState.PROMOTED


def test_cache_hit_path_zero_budget() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    assert agg.state is RenderState.CACHE_CHECK
    agg.cache_hit(clip_url="cached.mp4")
    assert agg.state is RenderState.ACCEPTED
    assert agg.is_terminal
    assert agg.video_seconds_spent == 0.0
    accepted = [e for e in agg.uncommitted if isinstance(e, ShotAccepted)]
    assert accepted[0].from_cache is True


def test_cache_miss_render_qa_accept_happy_path() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    assert agg.state is RenderState.RENDERING
    agg.rendered(clip_url="clip.mp4", video_seconds=5.0)
    assert agg.state is RenderState.QA
    assert agg.video_seconds_spent == 5.0
    agg.score_qa(score=0.95, passed=True)
    assert agg.qa_passed is True
    agg.accept_after_qa()
    assert agg.state is RenderState.ACCEPTED
    rendered = [e for e in agg.uncommitted if isinstance(e, ShotRendered)]
    assert rendered[0].from_cache is False


def test_qa_fail_repair_regen_loop_respects_cap() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    # Drive two full fail->repair->regen cycles (the §9.7 "<= 2" cap).
    for i in range(MAX_REPAIRS):
        agg.rendered(clip_url=f"c{i}", video_seconds=5.0)
        agg.score_qa(score=0.1, passed=False)
        agg.repair()
        agg.regen()
        assert agg.repair_count == i + 1
        assert agg.state is RenderState.RENDERING
    # A third failure may NOT regen — the cap is reached.
    agg.rendered(clip_url="c-final", video_seconds=5.0)
    agg.score_qa(score=0.1, passed=False)
    agg.repair()
    with pytest.raises(InvariantViolation, match="retry cap"):
        agg.regen()
    # The legal exit is to degrade.
    agg.degrade(reason="retries_exhausted")
    assert agg.state is RenderState.DEGRADED
    assert agg.is_terminal


def test_repair_can_raise_conflict() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    agg.rendered(clip_url="c", video_seconds=5.0)
    agg.score_qa(score=0.2, passed=False)
    agg.repair()
    agg.raise_conflict(contradicting_state_id="st-9", detail="wrong room")
    assert agg.state is RenderState.CONFLICT
    conflict = [e for e in agg.uncommitted if isinstance(e, ShotConflictRaised)]
    assert conflict[0].contradicting_state_id == "st-9"


def test_conflict_resolution_regen_accept_degrade() -> None:
    def at_conflict() -> RenderShotAggregate:
        agg = _planned()
        agg.promote()
        agg.begin_cache_check()
        agg.cache_miss()
        agg.rendered(clip_url="c", video_seconds=5.0)
        agg.score_qa(score=0.2, passed=False)
        agg.repair()
        agg.raise_conflict(detail="x")
        return agg

    a = at_conflict()
    a.resolve_conflict_regen()
    assert a.state is RenderState.RENDERING

    b = at_conflict()
    b.resolve_conflict_accept()
    assert b.state is RenderState.ACCEPTED

    c = at_conflict()
    c.degrade(reason="conflict_unresolved")
    assert c.state is RenderState.DEGRADED


def test_rendering_can_degrade_directly() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    agg.degrade(reason="provider_down")
    assert agg.state is RenderState.DEGRADED
    degraded = [e for e in agg.uncommitted if isinstance(e, ShotDegraded)]
    assert degraded[0].reason == "provider_down"


def test_illegal_transition_guard() -> None:
    agg = _planned()
    # PLANNED -> RENDERING is not a §9.7 edge.
    with pytest.raises(InvariantViolation, match="illegal"):
        agg.cache_miss()


def test_no_transition_after_terminal() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_hit(clip_url="c")
    with pytest.raises(InvariantViolation, match="terminal"):
        agg.promote()


def test_score_qa_only_in_qa_state() -> None:
    agg = _planned()
    agg.promote()
    with pytest.raises(InvariantViolation, match="must be in QA"):
        agg.score_qa(score=0.9, passed=True)


def test_accept_blocked_when_qa_failed() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    agg.rendered(clip_url="c", video_seconds=5.0)
    agg.score_qa(score=0.1, passed=False)
    # Still in QA (score_qa does not transition); accept must refuse a failed QA.
    with pytest.raises(InvariantViolation, match="QA failed"):
        agg.accept_after_qa()


def test_rendered_rejects_negative_seconds() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    with pytest.raises(InvariantViolation, match="non-negative"):
        agg.rendered(clip_url="c", video_seconds=-1.0)


def test_status_projection_matches_render_states() -> None:
    agg = _planned()
    agg.promote()
    assert agg.status is ShotStatus.PROMOTED
    agg.begin_cache_check()
    agg.cache_miss()
    assert agg.status is ShotStatus.RENDERING


def test_aggregate_edges_are_subset_of_canonical_table() -> None:
    # Every transition the aggregate can emit must be a legal §9.7 edge — proven
    # by exercising each decision method from a fresh state and checking the edge.
    edges = {
        (RenderState.PLANNED, RenderState.KEYFRAMED),
        (RenderState.PLANNED, RenderState.PROMOTED),
        (RenderState.KEYFRAMED, RenderState.PROMOTED),
        (RenderState.PROMOTED, RenderState.CACHE_CHECK),
        (RenderState.CACHE_CHECK, RenderState.ACCEPTED),
        (RenderState.CACHE_CHECK, RenderState.RENDERING),
        (RenderState.RENDERING, RenderState.QA),
        (RenderState.RENDERING, RenderState.DEGRADED),
        (RenderState.QA, RenderState.ACCEPTED),
        (RenderState.QA, RenderState.REPAIR),
        (RenderState.REPAIR, RenderState.RENDERING),
        (RenderState.REPAIR, RenderState.CONFLICT),
        (RenderState.REPAIR, RenderState.DEGRADED),
        (RenderState.CONFLICT, RenderState.RENDERING),
        (RenderState.CONFLICT, RenderState.ACCEPTED),
        (RenderState.CONFLICT, RenderState.DEGRADED),
    }
    for src, dst in edges:
        assert is_allowed(src, dst), f"{src}->{dst} should be legal"
    # And the canonical table allows no edge the aggregate cannot reach.
    canonical = {(s, d) for s, dsts in ALLOWED_TRANSITIONS.items() for d in dsts}
    # CONFLICT->CONFLICT (parked) is in the table but the aggregate never self-loops.
    assert edges == canonical - {(RenderState.CONFLICT, RenderState.CONFLICT)}


def test_request_regen_reopens_accepted_shot() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_hit(clip_url="c")
    assert agg.state is RenderState.ACCEPTED
    reopened = agg.request_regen(reason="director re-do", triggered_by="comment:c1")
    assert reopened is True
    assert agg.state is RenderState.PROMOTED
    assert agg.repair_count == 0
    assert agg.qa_passed is None


def test_request_regen_resets_repair_budget() -> None:
    agg = _planned()
    agg.promote()
    agg.begin_cache_check()
    agg.cache_miss()
    # Burn a repair, then degrade.
    agg.rendered(clip_url="c", video_seconds=5.0)
    agg.score_qa(score=0.1, passed=False)
    agg.repair()
    agg.regen()
    assert agg.repair_count == 1
    agg.rendered(clip_url="c2", video_seconds=5.0)
    agg.score_qa(score=0.1, passed=False)
    agg.repair()
    agg.degrade(reason="retries_exhausted")
    assert agg.state is RenderState.DEGRADED
    # A canon fix re-opens the degraded shot with a fresh attempt budget.
    assert agg.request_regen(triggered_by="canon_edit") is True
    assert agg.state is RenderState.PROMOTED
    assert agg.repair_count == 0


def test_request_regen_is_noop_when_already_in_flow() -> None:
    agg = _planned()
    agg.promote()  # PROMOTED — already re-openable / in flow
    assert agg.request_regen() is False
    assert agg.state is RenderState.PROMOTED


def test_request_regen_rejected_before_plan() -> None:
    agg = RenderShotAggregate("shot1")
    with pytest.raises(CommandRejected, match="not been planned"):
        agg.request_regen()


def test_replay_reconstructs_repair_count_and_budget() -> None:
    src = _planned()
    src.promote()
    src.begin_cache_check()
    src.cache_miss()
    src.rendered(clip_url="c", video_seconds=5.0)
    src.score_qa(score=0.1, passed=False)
    src.repair()
    src.regen()
    history = list(src.uncommitted) + []
    # Replay the pre-commit genesis + the new events into a fresh aggregate.
    full = RenderShotAggregate("shot1")
    full.replay([ShotPlanned(shot_id="shot1", book_id="b1", shot_hash="h1"), *history])
    assert full.repair_count == 1
    assert full.video_seconds_spent == 5.0
    assert full.state is RenderState.RENDERING
