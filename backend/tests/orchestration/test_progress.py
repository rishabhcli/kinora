"""Global progress / lag projection across the render fleet."""

from __future__ import annotations

from app.orchestration.models import (
    Lane,
    ShotLease,
    WorkerCapabilities,
    WorkerDescriptor,
    WorkerStatus,
)
from app.orchestration.progress import build_progress

from .conftest import caps, ticket


def _worker(
    wid: str,
    capabilities: WorkerCapabilities,
    *,
    hb: int = 0,
    status: WorkerStatus = WorkerStatus.ACTIVE,
) -> WorkerDescriptor:
    return WorkerDescriptor(
        worker_id=wid, capabilities=capabilities, status=status, last_heartbeat_ms=hb
    )


def _lease(
    shot: str,
    worker: str,
    *,
    lane: Lane = Lane.COMMITTED,
    provider: str = "wan",
    book: str = "b",
    expires: int = 100_000,
) -> ShotLease:
    return ShotLease(
        shot_hash=shot,
        worker_id=worker,
        fence=1,
        granted_at_ms=0,
        expires_at_ms=expires,
        lane=lane,
        provider=provider,
        book_id=book,
    )


def test_progress_counts_inflight_and_queued() -> None:
    workers = [_worker("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))]
    leases = [_lease("a", "w1"), _lease("b", "w1")]
    queued = [ticket("c", lane=Lane.SPECULATIVE), ticket("d", lane=Lane.SPECULATIVE)]
    fleet = build_progress(workers, leases, queued, now_ms=0, worker_ttl_ms=10_000)
    assert fleet.total_inflight == 2
    assert fleet.total_queued == 2
    assert fleet.total_capacity_free == 2  # 4 slots - 2 held


def test_per_lane_split() -> None:
    both = caps(Lane.COMMITTED, Lane.SPECULATIVE, providers=("wan",), max_concurrency=8)
    workers = [_worker("w1", both)]
    leases = [_lease("a", "w1", lane=Lane.COMMITTED), _lease("b", "w1", lane=Lane.SPECULATIVE)]
    queued = [ticket("c", lane=Lane.SPECULATIVE)]
    fleet = build_progress(workers, leases, queued, now_ms=0, worker_ttl_ms=10_000)
    by_lane = {lp.lane: lp for lp in fleet.lanes}
    assert by_lane[Lane.COMMITTED].inflight == 1
    assert by_lane[Lane.SPECULATIVE].inflight == 1
    assert by_lane[Lane.SPECULATIVE].queued == 1
    assert by_lane[Lane.SPECULATIVE].backlog == 2


def test_per_provider_inflight_and_video_seconds() -> None:
    workers = [_worker("w1", caps(Lane.COMMITTED, providers=("wan", "minimax"), max_concurrency=8))]
    leases = [_lease("a", "w1", provider="wan"), _lease("b", "w1", provider="minimax")]
    # video-seconds are surfaced via queued tickets sharing the shot_hash.
    queued = [ticket("a", provider="wan", video_seconds=6.0)]
    fleet = build_progress(workers, leases, queued, now_ms=0, worker_ttl_ms=10_000)
    by_provider = {pp.provider: pp for pp in fleet.providers}
    assert by_provider["wan"].inflight == 1
    assert by_provider["minimax"].inflight == 1
    assert by_provider["wan"].video_seconds_inflight == 6.0


def test_expired_leases_and_imbalance() -> None:
    workers = [
        _worker("busy", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8)),
        _worker("idle", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=8)),
    ]
    leases = [
        _lease("a", "busy", expires=100),
        _lease("b", "busy", expires=100),
        _lease("c", "busy", expires=100_000),  # still live
    ]
    fleet = build_progress(workers, leases, now_ms=5_000, worker_ttl_ms=10_000)
    assert fleet.expired_leases == 2  # a + b lapsed by t=5000
    # busy holds 3, idle holds 0 -> imbalance 3.
    assert fleet.load_imbalance == 3


def test_dead_worker_marked_not_live_in_view() -> None:
    workers = [_worker("w1", caps(Lane.COMMITTED, providers=("wan",)), hb=0)]
    fleet = build_progress(workers, [], now_ms=20_000, worker_ttl_ms=10_000)
    wp = fleet.workers[0]
    assert wp.is_live is False  # heartbeat older than TTL
    assert fleet.total_capacity_free == 0  # dead workers contribute no capacity


def test_utilization_and_as_dict_roundtrip() -> None:
    workers = [_worker("w1", caps(Lane.COMMITTED, providers=("wan",), max_concurrency=4))]
    leases = [_lease("a", "w1"), _lease("b", "w1"), _lease("c", "w1")]
    fleet = build_progress(workers, leases, now_ms=0, worker_ttl_ms=10_000)
    assert fleet.workers[0].utilization == 0.75  # 3 of 4
    d = fleet.as_dict()
    assert d["total_inflight"] == 3
    workers_view = d["workers"]
    assert isinstance(workers_view, list)
    assert workers_view[0]["utilization"] == 0.75
    assert isinstance(d["lanes"], list)
