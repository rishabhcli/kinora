"""Work-stealing planner: balances load idle ↔ backed-up, respects capability."""

from __future__ import annotations

from app.orchestration.models import (
    Lane,
    ShotLease,
    WorkerCapabilities,
    WorkerDescriptor,
    WorkerStatus,
)
from app.orchestration.rebalance import RebalanceConfig, Rebalancer

from .conftest import caps


def _worker(
    wid: str, capabilities: WorkerCapabilities, *, status: WorkerStatus = WorkerStatus.ACTIVE
) -> WorkerDescriptor:
    return WorkerDescriptor(worker_id=wid, capabilities=capabilities, status=status)


def _lease(
    shot: str,
    worker: str,
    *,
    lane: Lane = Lane.SPECULATIVE,
    provider: str = "wan",
    book: str = "b",
    at: int = 0,
) -> ShotLease:
    return ShotLease(
        shot_hash=shot,
        worker_id=worker,
        fence=1,
        granted_at_ms=at,
        expires_at_ms=at + 100_000,
        lane=lane,
        provider=provider,
        book_id=book,
    )


def test_balanced_fleet_plans_nothing() -> None:
    workers = [
        _worker("w1", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=4)),
        _worker("w2", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=4)),
    ]
    leases = [_lease("a", "w1"), _lease("b", "w2")]
    plan = Rebalancer(RebalanceConfig(imbalance_threshold=2)).plan(workers, leases)
    assert plan.is_empty


def test_steals_from_backed_up_to_idle() -> None:
    workers = [
        _worker("busy", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8)),
        _worker("idle", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8)),
    ]
    # busy holds 4, idle holds 0 -> gap 4 >= threshold 2.
    leases = [_lease(f"s{i}", "busy", at=i) for i in range(4)]
    plan = Rebalancer(RebalanceConfig(imbalance_threshold=2, max_steals=4)).plan(workers, leases)
    assert not plan.is_empty
    # All migrations move busy -> idle.
    assert all(m.from_worker == "busy" and m.to_worker == "idle" for m in plan.migrations)
    # It balances rather than over-shifts: it stops once the two are even (2 each).
    assert len(plan.migrations) == 2


def test_steal_respects_capability() -> None:
    # idle can only do KEYFRAME; busy's shots are SPECULATIVE -> nothing movable.
    workers = [
        _worker("busy", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8)),
        _worker("idle", caps(Lane.KEYFRAME, providers=("keyframe",), max_concurrency=8)),
    ]
    leases = [_lease(f"s{i}", "busy", at=i) for i in range(4)]
    plan = Rebalancer(RebalanceConfig(imbalance_threshold=2)).plan(workers, leases)
    assert plan.is_empty  # idle is incapable of the donor's lane


def test_committed_not_stolen_by_default() -> None:
    workers = [
        _worker("busy", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8)),
        _worker("idle", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8)),
    ]
    leases = [_lease(f"s{i}", "busy", lane=Lane.COMMITTED, at=i) for i in range(4)]
    plan = Rebalancer(RebalanceConfig(imbalance_threshold=2)).plan(workers, leases)
    assert plan.is_empty  # committed shots are sticky for continuity


def test_committed_stolen_when_explicitly_allowed() -> None:
    workers = [
        _worker("busy", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8)),
        _worker("idle", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8)),
    ]
    leases = [_lease(f"s{i}", "busy", lane=Lane.COMMITTED, at=i) for i in range(4)]
    plan = Rebalancer(
        RebalanceConfig(imbalance_threshold=2, steal_committed=True)
    ).plan(workers, leases)
    assert not plan.is_empty


def test_max_steals_caps_migrations() -> None:
    workers = [
        _worker("busy", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=20)),
        _worker("idle", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=20)),
    ]
    leases = [_lease(f"s{i}", "busy", at=i) for i in range(10)]
    plan = Rebalancer(RebalanceConfig(imbalance_threshold=2, max_steals=3)).plan(workers, leases)
    assert len(plan.migrations) == 3


def test_single_worker_fleet_never_steals() -> None:
    workers = [_worker("solo", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8))]
    leases = [_lease(f"s{i}", "solo", at=i) for i in range(5)]
    plan = Rebalancer().plan(workers, leases)
    assert plan.is_empty


def test_idle_worker_at_capacity_is_skipped() -> None:
    # 'idle' has free name but max_concurrency=0-equivalent via already-full slots.
    workers = [
        _worker("busy", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=8)),
        _worker("full", caps(Lane.SPECULATIVE, providers=("wan",), max_concurrency=1)),
    ]
    leases = [_lease(f"s{i}", "busy", at=i) for i in range(4)] + [_lease("x", "full")]
    plan = Rebalancer(RebalanceConfig(imbalance_threshold=2)).plan(workers, leases)
    # 'full' is at its cap (1/1); it can't receive work.
    assert plan.is_empty
