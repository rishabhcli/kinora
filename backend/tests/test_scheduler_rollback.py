"""Speculative-rollback tests (kinora.md §4.8/§4.6/§12.1) — pure, no infra.

Pin :class:`app.scheduler.rollback.SpeculationLedger`: a seek keeps near promotions
and rolls back distant ones; a skim rolls back everything; a direction flip keeps
what's behind it; rollback reclaims each reservation exactly once (idempotent);
confirmed promotions can't be rolled back; the ledger round-trips for persistence.
"""

from __future__ import annotations

from app.scheduler.rollback import (
    InvalidationReason,
    SpeculationLedger,
)

_TOKEN = "traj_abc"


def _ledger_with(*words: int, token: str = _TOKEN, dur: float = 5.0) -> SpeculationLedger:
    ledger = SpeculationLedger()
    for i, w in enumerate(words):
        ledger.record(
            job_id=f"job_{i}",
            shot_id=f"shot_{i}",
            reservation_id=f"res_{i}",
            word_index_start=w,
            reserved_video_s=dur,
            trajectory_token=token,
        )
    return ledger


def test_records_and_tracks_outstanding() -> None:
    ledger = _ledger_with(100, 200, 300)
    assert len(ledger.outstanding) == 3
    assert ledger.outstanding_video_s == 15.0


# --- seek: keep near, roll back distant (§4.8) ----------------------------- #


def test_seek_keeps_near_promotions_and_rolls_back_distant() -> None:
    # Promotions at words 40, 80 (near 50) and 4000 (far). Seek to word 50.
    ledger = _ledger_with(40, 80, 4000)
    plan = ledger.plan_rollback(
        reason=InvalidationReason.SEEK,
        new_word=50,
        old_token=_TOKEN,
        keep_threshold_s=120.0,
        velocity_wps=4.0,
    )
    kept = {p.word_index_start for p in plan.keep}
    cancelled = {p.word_index_start for p in plan.cancel}
    assert kept == {40, 80}  # within 120s of reading-time at 4 wps
    assert cancelled == {4000}  # ~987s away → rolled back
    assert plan.reclaimed_video_s == 5.0


def test_backward_seek_into_buffered_span_is_a_cache_hit() -> None:
    # Reader seeks *back* to word 90; nearby promotions are kept (re-read cache).
    ledger = _ledger_with(100, 120, 150)
    plan = ledger.plan_rollback(
        reason=InvalidationReason.SEEK, new_word=90, old_token=_TOKEN, velocity_wps=4.0
    )
    assert plan.cancel == []  # all within the keep window → nothing wasted
    assert len(plan.keep) == 3


# --- skim: roll back everything (§4.6) ------------------------------------- #


def test_skim_rolls_back_all_outstanding() -> None:
    ledger = _ledger_with(40, 80, 120)
    plan = ledger.plan_rollback(
        reason=InvalidationReason.SKIM, new_word=50, old_token=_TOKEN
    )
    assert plan.keep == []
    assert len(plan.cancel) == 3
    assert plan.reclaimed_video_s == 15.0


# --- direction flip: keep behind, drop ahead (§4.7) ------------------------ #


def test_direction_flip_keeps_behind_drops_ahead() -> None:
    ledger = _ledger_with(80, 120, 200)
    plan = ledger.plan_rollback(
        reason=InvalidationReason.DIRECTION_FLIP, new_word=100, old_token=_TOKEN
    )
    kept = {p.word_index_start for p in plan.keep}
    cancelled = {p.word_index_start for p in plan.cancel}
    assert kept == {80}  # behind/at the flip → already-read, keep
    assert cancelled == {120, 200}  # the rejected forward path → roll back


# --- token discipline ------------------------------------------------------ #


def test_old_event_never_rolls_back_a_newer_token() -> None:
    ledger = _ledger_with(40, 4000)
    # A promotion under a *newer* token (already re-seeded).
    ledger.record(
        job_id="job_new",
        shot_id="shot_new",
        reservation_id="res_new",
        word_index_start=5000,
        reserved_video_s=5.0,
        trajectory_token="traj_new",
    )
    plan = ledger.plan_rollback(
        reason=InvalidationReason.SKIM, new_word=50, old_token=_TOKEN
    )
    assert all(p.trajectory_token == _TOKEN for p in plan.cancel)
    assert any(p.job_id == "job_new" for p in plan.keep)  # newer token preserved


# --- idempotent apply (no double-credit) ----------------------------------- #


def test_apply_reclaims_each_reservation_exactly_once() -> None:
    ledger = _ledger_with(40, 80, 4000)
    plan = ledger.plan_rollback(
        reason=InvalidationReason.SEEK, new_word=50, old_token=_TOKEN, velocity_wps=4.0
    )
    first = ledger.apply(plan)
    second = ledger.apply(plan)  # re-applying must reclaim nothing more
    assert first == 5.0
    assert second == 0.0
    # The cancelled entry is gone; the kept ones remain outstanding.
    assert {p.word_index_start for p in ledger.outstanding} == {40, 80}


def test_confirmed_promotion_cannot_be_rolled_back() -> None:
    ledger = _ledger_with(40, 4000)
    ledger.confirm("job_1")  # reader reached the word-4000 shot's predecessor
    plan = ledger.plan_rollback(
        reason=InvalidationReason.SKIM, new_word=50, old_token=_TOKEN
    )
    assert all(p.word_index_start != 4000 for p in plan.cancel)


# --- persistence ----------------------------------------------------------- #


def test_ledger_round_trips_through_state() -> None:
    ledger = _ledger_with(40, 80, 4000)
    restored = SpeculationLedger.from_state(ledger.to_state())
    assert restored.outstanding_video_s == ledger.outstanding_video_s
    assert {p.job_id for p in restored.outstanding} == {p.job_id for p in ledger.outstanding}
