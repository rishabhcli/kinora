"""Pure placement policy: capability filter, provider capacity, locality, load."""

from __future__ import annotations

from app.orchestration.capacity import StaticCapacityOracle
from app.orchestration.models import (
    Lane,
    WorkerCapabilities,
    WorkerDescriptor,
    WorkerStatus,
)
from app.orchestration.placement import WorkerLoad, choose_worker, score_candidates

from .conftest import caps, ticket


def _worker(
    wid: str, capabilities: WorkerCapabilities, *, status: WorkerStatus = WorkerStatus.ACTIVE
) -> WorkerDescriptor:
    return WorkerDescriptor(worker_id=wid, capabilities=capabilities, status=status)


def test_capability_filters_wrong_lane() -> None:
    keyframe_only = _worker("w1", caps(Lane.KEYFRAME, providers=("wan",)))
    chosen = choose_worker(
        ticket("s", lane=Lane.COMMITTED, provider="wan"),
        [keyframe_only],
        {},
        oracle=StaticCapacityOracle(max_inflight={"wan": 4}),
    )
    assert chosen is None  # worker can't serve the committed lane


def test_capability_filters_wrong_provider() -> None:
    minimax_only = _worker("w1", caps(Lane.COMMITTED, providers=("minimax",)))
    chosen = choose_worker(
        ticket("s", provider="wan"),
        [minimax_only],
        {},
        oracle=StaticCapacityOracle(max_inflight={"wan": 4}),
    )
    assert chosen is None


def test_provider_agnostic_worker_serves_any_provider() -> None:
    agnostic = _worker("w1", caps(Lane.COMMITTED))  # no providers advertised
    chosen = choose_worker(
        ticket("s", provider="some-new-provider"),
        [agnostic],
        {},
        oracle=StaticCapacityOracle(default_max_inflight=4),
    )
    assert chosen == "w1"


def test_provider_capacity_blocks_when_no_slots() -> None:
    w = _worker("w1", caps(Lane.COMMITTED, providers=("wan",)))
    oracle = StaticCapacityOracle(max_inflight={"wan": 1})
    oracle.note_assigned("wan", video_seconds=5)  # the only slot is taken
    chosen = choose_worker(ticket("s", provider="wan"), [w], {}, oracle=oracle)
    assert chosen is None


def test_provider_capacity_blocks_when_video_seconds_exhausted() -> None:
    w = _worker("w1", caps(Lane.COMMITTED, providers=("wan",)))
    oracle = StaticCapacityOracle(
        max_inflight={"wan": 4}, video_seconds_headroom={"wan": 3.0}
    )
    chosen = choose_worker(ticket("s", provider="wan", video_seconds=5.0), [w], {}, oracle=oracle)
    assert chosen is None  # 5s doesn't fit in 3s of headroom


def test_least_loaded_wins_among_equals() -> None:
    busy = _worker("busy", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    idle = _worker("idle", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    loads = {
        "busy": WorkerLoad("busy", leases_held=3, leases_for_book=0),
        "idle": WorkerLoad("idle", leases_held=0, leases_for_book=0),
    }
    chosen = choose_worker(
        ticket("s", book_id="other-book", provider="wan"),
        [busy, idle],
        loads,
        oracle=StaticCapacityOracle(max_inflight={"wan": 8}),
    )
    assert chosen == "idle"


def test_sticky_owner_beats_a_freer_worker() -> None:
    # The book's owner is busier, but locality should keep the book on it.
    owner = _worker("owner", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    fresh = _worker("fresh", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    loads = {
        "owner": WorkerLoad("owner", leases_held=2, leases_for_book=2),
        "fresh": WorkerLoad("fresh", leases_held=0, leases_for_book=0),
    }
    chosen = choose_worker(
        ticket("s", book_id="book-1", provider="wan"),
        [owner, fresh],
        loads,
        oracle=StaticCapacityOracle(max_inflight={"wan": 8}),
        sticky_book_owner="owner",
    )
    assert chosen == "owner"  # continuity wins


def test_sticky_owner_full_falls_through_to_another() -> None:
    owner = _worker("owner", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=2))
    fresh = _worker("fresh", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=2))
    loads = {
        "owner": WorkerLoad("owner", leases_held=2, leases_for_book=2),  # full
        "fresh": WorkerLoad("fresh", leases_held=0, leases_for_book=0),
    }
    chosen = choose_worker(
        ticket("s", book_id="book-1", provider="wan"),
        [owner, fresh],
        loads,
        oracle=StaticCapacityOracle(max_inflight={"wan": 8}),
        sticky_book_owner="owner",
    )
    assert chosen == "fresh"  # owner has no slots, re-home


def test_draining_worker_is_not_chosen() -> None:
    draining = _worker(
        "w1", caps(Lane.COMMITTED, providers=("wan",)), status=WorkerStatus.DRAINING
    )
    chosen = choose_worker(
        ticket("s", provider="wan"),
        [draining],
        {},
        oracle=StaticCapacityOracle(max_inflight={"wan": 4}),
    )
    assert chosen is None


def test_tie_break_is_deterministic_smallest_id() -> None:
    a = _worker("aaa", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    z = _worker("zzz", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))
    oracle = StaticCapacityOracle(max_inflight={"wan": 8})
    t = ticket("s", book_id="x", provider="wan")
    # Same score (both idle, neither sticky) -> smallest worker_id wins, repeatably.
    assert choose_worker(t, [a, z], {}, oracle=oracle) == "aaa"
    assert choose_worker(t, [z, a], {}, oracle=oracle) == "aaa"


def test_score_candidates_empty_when_provider_cannot_admit() -> None:
    w = _worker("w1", caps(Lane.COMMITTED, providers=("wan",)))
    oracle = StaticCapacityOracle(max_inflight={"wan": 0})
    assert score_candidates(ticket("s", provider="wan"), [w], {}, oracle=oracle) == []
