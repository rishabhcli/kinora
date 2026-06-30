"""Global progress / lag view across the render fleet (kinora.md §12.5).

The §12.5 observability story is per-shot and per-session; this is the *fleet*
view the orchestrator needs to reason about itself: how many shots are in flight,
how they spread across workers and providers, which leases are at risk of
expiring (a worker that stopped heartbeating), and where the imbalance is.

:func:`build_progress` is a pure projection of (registry workers, live leases,
queued tickets) at a point in time — no I/O, no clock side effects (it takes
``now_ms``). The coordinator / a metrics endpoint calls it to publish a snapshot;
tests call it with hand-built inputs and assert the numbers.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.orchestration.models import (
    Lane,
    ShotLease,
    ShotTicket,
    WorkerDescriptor,
    WorkerStatus,
)

__all__ = [
    "WorkerProgress",
    "LaneProgress",
    "ProviderProgress",
    "FleetProgress",
    "build_progress",
]


@dataclass(frozen=True, slots=True)
class WorkerProgress:
    """Per-worker slice of the fleet view."""

    worker_id: str
    status: WorkerStatus
    leases_held: int
    free_slots: int
    #: ms since this worker's last heartbeat (staleness).
    heartbeat_age_ms: int
    is_live: bool

    @property
    def utilization(self) -> float:
        """Fraction of local slots in use (0..1); 0 when the worker has no slots."""
        total = self.leases_held + self.free_slots
        return self.leases_held / total if total else 0.0


@dataclass(frozen=True, slots=True)
class LaneProgress:
    """Per-lane in-flight + queued counts."""

    lane: Lane
    inflight: int
    queued: int

    @property
    def backlog(self) -> int:
        return self.inflight + self.queued


@dataclass(frozen=True, slots=True)
class ProviderProgress:
    """Per-provider in-flight count + video-seconds committed to live leases."""

    provider: str
    inflight: int
    video_seconds_inflight: float


@dataclass(frozen=True, slots=True)
class FleetProgress:
    """A point-in-time global view of the render fleet."""

    now_ms: int
    workers: tuple[WorkerProgress, ...]
    lanes: tuple[LaneProgress, ...]
    providers: tuple[ProviderProgress, ...]
    total_inflight: int
    total_queued: int
    #: Leases past expiry at ``now_ms`` (recovery is owed — a sweep will reclaim).
    expired_leases: int
    #: Max-minus-min leases across live workers (0 = perfectly balanced).
    load_imbalance: int

    @property
    def total_capacity_free(self) -> int:
        return sum(w.free_slots for w in self.workers if w.is_live)

    def as_dict(self) -> dict[str, object]:
        return {
            "now_ms": self.now_ms,
            "total_inflight": self.total_inflight,
            "total_queued": self.total_queued,
            "expired_leases": self.expired_leases,
            "load_imbalance": self.load_imbalance,
            "capacity_free": self.total_capacity_free,
            "workers": [
                {
                    "worker_id": w.worker_id,
                    "status": w.status.value,
                    "leases_held": w.leases_held,
                    "free_slots": w.free_slots,
                    "utilization": round(w.utilization, 3),
                    "heartbeat_age_ms": w.heartbeat_age_ms,
                    "is_live": w.is_live,
                }
                for w in self.workers
            ],
            "lanes": [
                {"lane": lp.lane.value, "inflight": lp.inflight, "queued": lp.queued}
                for lp in self.lanes
            ],
            "providers": [
                {
                    "provider": pp.provider,
                    "inflight": pp.inflight,
                    "video_seconds_inflight": round(pp.video_seconds_inflight, 3),
                }
                for pp in self.providers
            ],
        }


def build_progress(
    workers: Sequence[WorkerDescriptor],
    leases: Sequence[ShotLease],
    queued: Sequence[ShotTicket] = (),
    *,
    now_ms: int,
    worker_ttl_ms: int,
) -> FleetProgress:
    """Project (workers, leases, queued tickets) into a :class:`FleetProgress`."""
    leases_by_worker: dict[str, int] = {}
    for lease in leases:
        leases_by_worker[lease.worker_id] = leases_by_worker.get(lease.worker_id, 0) + 1

    # Per-worker slice.
    worker_views: list[WorkerProgress] = []
    live_counts: list[int] = []
    for worker in sorted(workers, key=lambda w: w.worker_id):
        held = leases_by_worker.get(worker.worker_id, 0)
        free = max(0, worker.capabilities.max_concurrency - held)
        age = now_ms - worker.last_heartbeat_ms
        is_live = worker.is_live(now_ms=now_ms, ttl_ms=worker_ttl_ms)
        worker_views.append(
            WorkerProgress(
                worker_id=worker.worker_id,
                status=worker.status,
                leases_held=held,
                free_slots=free,
                heartbeat_age_ms=age,
                is_live=is_live,
            )
        )
        if is_live:
            live_counts.append(held)

    # Per-lane: inflight from leases, queued from tickets.
    inflight_by_lane: dict[Lane, int] = {}
    inflight_by_provider: dict[str, int] = {}
    vs_by_provider: dict[str, float] = {}
    for lease in leases:
        inflight_by_lane[lease.lane] = inflight_by_lane.get(lease.lane, 0) + 1
        inflight_by_provider[lease.provider] = inflight_by_provider.get(lease.provider, 0) + 1
    queued_by_lane: dict[Lane, int] = {}
    for ticket in queued:
        queued_by_lane[ticket.lane] = queued_by_lane.get(ticket.lane, 0) + 1

    # Provider in-flight video-seconds: leases don't carry the estimate, so we map
    # via any queued ticket that shares the shot_hash (best-effort), else 0. This
    # surfaces provider spend pressure when the caller passes the ticket pool.
    queued_vs = {t.shot_hash: t.video_seconds for t in queued}
    for lease in leases:
        vs_by_provider[lease.provider] = vs_by_provider.get(lease.provider, 0.0) + queued_vs.get(
            lease.shot_hash, 0.0
        )

    all_lanes = set(inflight_by_lane) | set(queued_by_lane)
    lane_views = tuple(
        LaneProgress(
            lane=lane,
            inflight=inflight_by_lane.get(lane, 0),
            queued=queued_by_lane.get(lane, 0),
        )
        for lane in sorted(all_lanes, key=lambda lane: lane.value)
    )
    provider_views = tuple(
        ProviderProgress(
            provider=provider,
            inflight=inflight_by_provider[provider],
            video_seconds_inflight=vs_by_provider.get(provider, 0.0),
        )
        for provider in sorted(inflight_by_provider)
    )

    expired = sum(1 for lease in leases if lease.is_expired(now_ms=now_ms))
    imbalance = (max(live_counts) - min(live_counts)) if live_counts else 0

    return FleetProgress(
        now_ms=now_ms,
        workers=tuple(worker_views),
        lanes=lane_views,
        providers=provider_views,
        total_inflight=len(leases),
        total_queued=len(queued),
        expired_leases=expired,
        load_imbalance=imbalance,
    )
