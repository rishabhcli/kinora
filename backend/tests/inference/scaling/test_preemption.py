"""Unit tests for priority preemption (app.inference.scaling.preemption)."""

from __future__ import annotations

import pytest

from app.inference.scaling.preemption import (
    InflightJob,
    PreemptionOutcome,
    PreemptionPlanner,
    PreemptionPolicy,
)
from app.inference.scaling.workload import RequestPriority


def _spec(job_id: str, elapsed: float, total: float = 5.0) -> InflightJob:
    return InflightJob(
        job_id=job_id, priority=RequestPriority.SPECULATIVE, elapsed_s=elapsed, total_s=total
    )


def _commit(job_id: str, elapsed: float, total: float = 5.0) -> InflightJob:
    return InflightJob(
        job_id=job_id, priority=RequestPriority.COMMITTED, elapsed_s=elapsed, total_s=total
    )


# --------------------------------------------------------------------------- #
# InflightJob math
# --------------------------------------------------------------------------- #


def test_progress_and_remaining() -> None:
    j = _spec("a", elapsed=2.0, total=8.0)
    assert j.progress == pytest.approx(0.25)
    assert j.remaining_s == pytest.approx(6.0)


def test_progress_clamps_at_one() -> None:
    j = _spec("a", elapsed=10.0, total=5.0)
    assert j.progress == 1.0
    assert j.remaining_s == 0.0


# --------------------------------------------------------------------------- #
# No preemption when not needed / not eligible
# --------------------------------------------------------------------------- #


def test_free_slot_means_no_preemption() -> None:
    planner = PreemptionPlanner()
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED,
        inflight=[_spec("a", 1.0)],
        has_free_slot=True,
    )
    assert d.outcome is PreemptionOutcome.NONE


def test_speculative_arrival_never_preempts() -> None:
    planner = PreemptionPlanner()
    d = planner.plan(
        arrival_priority=RequestPriority.SPECULATIVE,
        inflight=[_spec("a", 1.0)],
        has_free_slot=False,
    )
    assert d.outcome is PreemptionOutcome.NONE


def test_disabled_planner_never_preempts() -> None:
    planner = PreemptionPlanner(PreemptionPolicy(enabled=False))
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED,
        inflight=[_spec("a", 1.0)],
        has_free_slot=False,
    )
    assert d.outcome is PreemptionOutcome.NONE


# --------------------------------------------------------------------------- #
# Preemption selection
# --------------------------------------------------------------------------- #


def test_committed_preempts_youngest_speculative() -> None:
    planner = PreemptionPlanner(
        PreemptionPolicy(max_victim_progress=0.9, min_victim_remaining_s=0.0)
    )
    inflight = [_spec("old", elapsed=4.0, total=10.0), _spec("young", elapsed=1.0, total=10.0)]
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED, inflight=inflight, has_free_slot=False
    )
    assert d.outcome is PreemptionOutcome.PREEMPT
    assert d.victim_id == "young"  # least wasted work
    assert d.wasted_s == pytest.approx(1.0)


def test_no_speculative_victim_queues_committed() -> None:
    planner = PreemptionPlanner()
    inflight = [_commit("c1", 1.0), _commit("c2", 2.0)]
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED, inflight=inflight, has_free_slot=False
    )
    assert d.outcome is PreemptionOutcome.QUEUE
    assert d.victim_id is None


def test_almost_done_victim_is_protected() -> None:
    # A speculative job 90% done is not worth preempting (max_victim_progress 0.8).
    planner = PreemptionPlanner(
        PreemptionPolicy(max_victim_progress=0.8, min_victim_remaining_s=0.0)
    )
    inflight = [_spec("nearly", elapsed=9.0, total=10.0)]
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED, inflight=inflight, has_free_slot=False
    )
    assert d.outcome is PreemptionOutcome.QUEUE


def test_soon_finishing_victim_is_protected() -> None:
    planner = PreemptionPlanner(
        PreemptionPolicy(min_victim_remaining_s=2.0, max_victim_progress=1.0)
    )
    inflight = [_spec("soon", elapsed=9.5, total=10.0)]  # 0.5s remaining < 2.0
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED, inflight=inflight, has_free_slot=False
    )
    assert d.outcome is PreemptionOutcome.QUEUE


def test_decision_to_dict() -> None:
    planner = PreemptionPlanner(PreemptionPolicy(min_victim_remaining_s=0.0))
    d = planner.plan(
        arrival_priority=RequestPriority.COMMITTED,
        inflight=[_spec("a", 1.0, total=10.0)],
        has_free_slot=False,
    )
    payload = d.to_dict()
    assert payload["outcome"] == "preempt"
    assert payload["victim_id"] == "a"


def test_policy_rejects_bad_progress() -> None:
    with pytest.raises(ValueError):
        PreemptionPolicy(max_victim_progress=0.0)
    with pytest.raises(ValueError):
        PreemptionPolicy(max_victim_progress=1.5)
