"""The worker registry + heartbeat / dead-worker model (kinora.md §12.1/§12.2).

Many render workers come and go (autoscaling, deploys, crashes). The registry is
the orchestrator's view of *who is alive and what they can do*:

* **register** — a worker announces itself with its :class:`WorkerCapabilities`
  (the lanes + providers it can serve, its local slot count);
* **heartbeat** — a periodic liveness ping; a worker whose heartbeat lapses past
  ``worker_ttl_ms`` is considered DEAD;
* **drain** — graceful shutdown: stop offering new work but honour live leases;
* **sweep** — detect dead workers and reclaim their leases so the coordinator can
  reassign the orphaned shots (crash recovery).

The registry holds no render logic and no clock of its own — it reads "now" from
the injected :class:`Clock` and persists through the :class:`OrchestrationStore`,
so dead-worker detection + lease reclamation are deterministic under a virtual
clock with fake workers.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.orchestration.clock import Clock
from app.orchestration.models import (
    ShotLease,
    WorkerCapabilities,
    WorkerDescriptor,
    WorkerStatus,
)
from app.orchestration.store import OrchestrationStore

logger = structlog.get_logger("app.orchestration.registry")

__all__ = ["RegistryConfig", "SweepReport", "WorkerRegistry"]


@dataclass(frozen=True, slots=True)
class RegistryConfig:
    """Timing knobs for liveness (all in ms; mirror queue lease cadence)."""

    #: A worker silent longer than this is DEAD; its leases are reclaimed.
    worker_ttl_ms: int = 90_000
    #: Default lease window granted on acquire (well over a render, like §12.1).
    lease_ttl_ms: int = 120_000

    def __post_init__(self) -> None:
        if self.worker_ttl_ms <= 0 or self.lease_ttl_ms <= 0:
            raise ValueError("ttl values must be positive")


@dataclass(frozen=True, slots=True)
class SweepReport:
    """The outcome of one dead-worker sweep."""

    #: Workers transitioned to DEAD this sweep.
    dead_workers: tuple[str, ...]
    #: Leases reclaimed from dead workers (now free for reassignment).
    reclaimed_leases: tuple[ShotLease, ...]

    @property
    def any_recovery(self) -> bool:
        return bool(self.dead_workers or self.reclaimed_leases)


class WorkerRegistry:
    """Tracks live workers + reclaims the leases of the dead ones."""

    def __init__(
        self,
        store: OrchestrationStore,
        *,
        clock: Clock,
        config: RegistryConfig | None = None,
    ) -> None:
        self._store = store
        self._clock = clock
        self._cfg = config or RegistryConfig()

    @property
    def config(self) -> RegistryConfig:
        return self._cfg

    # -- lifecycle ----------------------------------------------------------- #

    async def register(
        self,
        worker_id: str,
        capabilities: WorkerCapabilities,
        *,
        region: str | None = None,
    ) -> WorkerDescriptor:
        """Register (or re-register) a worker as ACTIVE with a fresh heartbeat."""
        now = self._clock.now_ms()
        worker = WorkerDescriptor(
            worker_id=worker_id,
            capabilities=capabilities,
            status=WorkerStatus.ACTIVE,
            last_heartbeat_ms=now,
            region=region,
        )
        await self._store.put_worker(worker)
        logger.info(
            "registry.register",
            worker_id=worker_id,
            lanes=sorted(lane.value for lane in capabilities.lanes),
            providers=sorted(capabilities.providers),
            region=region,
        )
        return worker

    async def heartbeat(self, worker_id: str) -> WorkerDescriptor | None:
        """Refresh a worker's liveness. Resurrects a DEAD worker back to ACTIVE.

        Returns ``None`` if the worker was never registered (a stray heartbeat).
        A DRAINING worker stays DRAINING (it asked to stop taking work); a DEAD
        worker that pings again is brought back ACTIVE — it evidently survived.
        """
        existing = await self._store.get_worker(worker_id)
        if existing is None:
            logger.warning("registry.heartbeat_unknown", worker_id=worker_id)
            return None
        now = self._clock.now_ms()
        status = WorkerStatus.ACTIVE if existing.status is WorkerStatus.DEAD else existing.status
        refreshed = existing.model_copy(update={"last_heartbeat_ms": now, "status": status})
        await self._store.put_worker(refreshed)
        return refreshed

    async def drain(self, worker_id: str) -> WorkerDescriptor | None:
        """Mark a worker DRAINING — no new assignments, live leases honoured."""
        existing = await self._store.get_worker(worker_id)
        if existing is None:
            return None
        draining = existing.model_copy(update={"status": WorkerStatus.DRAINING})
        await self._store.put_worker(draining)
        logger.info("registry.drain", worker_id=worker_id)
        return draining

    async def deregister(self, worker_id: str) -> None:
        """Remove a worker record entirely (clean shutdown after draining)."""
        await self._store.remove_worker(worker_id)
        logger.info("registry.deregister", worker_id=worker_id)

    # -- views --------------------------------------------------------------- #

    async def live_workers(self) -> list[WorkerDescriptor]:
        """Workers whose heartbeat is within TTL and not DEAD."""
        now = self._clock.now_ms()
        ttl = self._cfg.worker_ttl_ms
        return [w for w in await self._store.list_workers() if w.is_live(now_ms=now, ttl_ms=ttl)]

    async def assignable_workers(self) -> list[WorkerDescriptor]:
        """Live workers that accept *new* work (ACTIVE, not DRAINING/DEAD)."""
        return [w for w in await self.live_workers() if w.accepts_work()]

    # -- crash recovery ------------------------------------------------------ #

    async def sweep(self) -> SweepReport:
        """Detect dead workers, mark them DEAD, and reclaim their leases.

        A worker is dead when its heartbeat is older than ``worker_ttl_ms``. Its
        leases are reclaimed by releasing them at the store's *current* fence, so a
        subsequent re-acquire by another worker advances the fence — the original
        worker, if it ever wakes, is fenced out (:class:`FenceViolationError`). We also
        reap any lease that has expired on its own (a slow render that missed its
        heartbeat) even if its owner still looks alive, matching §12.1 visibility-
        timeout semantics.
        """
        now = self._clock.now_ms()
        ttl = self._cfg.worker_ttl_ms
        workers = await self._store.list_workers()

        dead_ids: set[str] = set()
        for worker in workers:
            if worker.status is WorkerStatus.DEAD:
                dead_ids.add(worker.worker_id)
                continue
            if (now - worker.last_heartbeat_ms) > ttl:
                dead = worker.model_copy(update={"status": WorkerStatus.DEAD})
                await self._store.put_worker(dead)
                dead_ids.add(worker.worker_id)
                logger.warning("registry.worker_dead", worker_id=worker.worker_id)

        # Reclaim: any lease whose owner is dead, or that has expired on its own.
        reclaimed: list[ShotLease] = []
        for lease in await self._store.list_leases():
            orphaned = lease.worker_id in dead_ids
            expired = lease.is_expired(now_ms=now)
            if not (orphaned or expired):
                continue
            try:
                released = await self._store.release(shot_hash=lease.shot_hash, fence=lease.fence)
            except Exception as exc:  # noqa: BLE001 - a racing reassign already moved it
                logger.info("registry.reclaim_skipped", shot_hash=lease.shot_hash, error=str(exc))
                continue
            if released:
                reclaimed.append(lease)
                logger.info(
                    "registry.lease_reclaimed",
                    shot_hash=lease.shot_hash,
                    worker_id=lease.worker_id,
                    reason="dead" if orphaned else "expired",
                )

        return SweepReport(
            dead_workers=tuple(sorted(dead_ids)),
            reclaimed_leases=tuple(reclaimed),
        )
