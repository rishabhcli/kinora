"""The orchestration store seam + an in-memory implementation (kinora.md §12.1).

The registry and coordinator are *pure coordination logic*; they never touch
Redis directly. They go through :class:`OrchestrationStore` — a narrow async
protocol over the two pieces of distributed state the layer owns:

* the **worker table** (who is registered, their capabilities, last heartbeat);
* the **lease table** (which worker holds which shot, with a fence token).

A production adapter would back this with Redis hashes + a Lua compare-and-set on
the fence (mirroring the queue's claim script). :class:`InMemoryOrchestrationStore`
implements the same contract in plain Python under an ``asyncio.Lock`` so the
whole subsystem is testable with **zero infra** — and the single-renderer
invariant (fence CAS) is enforced identically in memory and in Redis.

Concurrency model: the in-memory store guards every mutation with one lock, so
even under simulated contention (many fake workers racing for the same shot)
exactly one acquire wins — the property the coordinator tests assert.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from app.orchestration.clock import Clock
from app.orchestration.models import (
    FenceViolationError,
    ShotLease,
    WorkerDescriptor,
)

__all__ = ["OrchestrationStore", "InMemoryOrchestrationStore"]


class OrchestrationStore(Protocol):
    """Distributed state the orchestration layer reads/writes (registry + leases).

    All methods are async (a Redis adapter is async); the in-memory impl satisfies
    them trivially. Lease mutations enforce the fence: a write with a fence that
    does not match the lease's current fence raises :class:`FenceViolationError`.
    """

    # -- worker table -------------------------------------------------------- #

    async def put_worker(self, worker: WorkerDescriptor) -> None:
        """Upsert a worker record (register or heartbeat)."""
        ...

    async def get_worker(self, worker_id: str) -> WorkerDescriptor | None:
        """Fetch one worker record, or ``None`` if unknown."""
        ...

    async def list_workers(self) -> list[WorkerDescriptor]:
        """Snapshot of every registered worker."""
        ...

    async def remove_worker(self, worker_id: str) -> None:
        """Drop a worker record entirely (deregister)."""
        ...

    # -- lease table --------------------------------------------------------- #

    async def try_acquire(
        self,
        *,
        shot_hash: str,
        worker_id: str,
        lane: object,
        provider: str,
        book_id: str,
        now_ms: int,
        ttl_ms: int,
    ) -> ShotLease | None:
        """Atomically acquire the lease for ``shot_hash`` if it is free/expired.

        Returns the granted :class:`ShotLease` (with an advanced fence) on success,
        or ``None`` if another worker holds a live lease. This is the single-
        renderer choke point: at most one caller can win for a given shot.
        """
        ...

    async def get_lease(self, shot_hash: str) -> ShotLease | None:
        """The current lease on ``shot_hash``, or ``None`` if unleased."""
        ...

    async def extend(self, *, shot_hash: str, fence: int, now_ms: int, ttl_ms: int) -> ShotLease:
        """Heartbeat: push the expiry out. Rejects a stale ``fence``."""
        ...

    async def release(self, *, shot_hash: str, fence: int) -> bool:
        """Release a held lease (completion / cancel). Rejects a stale ``fence``."""
        ...

    async def list_leases(self) -> list[ShotLease]:
        """Snapshot of every live lease."""
        ...

    async def reap_expired(self, *, now_ms: int) -> list[ShotLease]:
        """Remove and return every lease whose expiry has passed (crash recovery)."""
        ...


class InMemoryOrchestrationStore:
    """A dependency-free async store for tests + single-process orchestration.

    Backed by two dicts under one :class:`asyncio.Lock`. The fence counter is
    *per shot* and strictly increasing across the lifetime of the store, so a
    reassigned shot always gets a higher fence than the lease it replaced — even
    after the prior lease expired and was reaped.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._workers: dict[str, WorkerDescriptor] = {}
        self._leases: dict[str, ShotLease] = {}
        #: Highest fence ever issued per shot; survives reap so fences never reuse.
        self._fence_hwm: dict[str, int] = {}
        self._lock = asyncio.Lock()

    # -- worker table -------------------------------------------------------- #

    async def put_worker(self, worker: WorkerDescriptor) -> None:
        async with self._lock:
            self._workers[worker.worker_id] = worker

    async def get_worker(self, worker_id: str) -> WorkerDescriptor | None:
        async with self._lock:
            return self._workers.get(worker_id)

    async def list_workers(self) -> list[WorkerDescriptor]:
        async with self._lock:
            return list(self._workers.values())

    async def remove_worker(self, worker_id: str) -> None:
        async with self._lock:
            self._workers.pop(worker_id, None)

    # -- lease table --------------------------------------------------------- #

    async def try_acquire(
        self,
        *,
        shot_hash: str,
        worker_id: str,
        lane: object,
        provider: str,
        book_id: str,
        now_ms: int,
        ttl_ms: int,
    ) -> ShotLease | None:
        async with self._lock:
            existing = self._leases.get(shot_hash)
            if existing is not None and now_ms < existing.expires_at_ms:
                # A live lease is held — single-renderer invariant: deny.
                return None
            fence = self._fence_hwm.get(shot_hash, 0) + 1
            self._fence_hwm[shot_hash] = fence
            from app.orchestration.models import Lane

            lease = ShotLease(
                shot_hash=shot_hash,
                worker_id=worker_id,
                fence=fence,
                granted_at_ms=now_ms,
                expires_at_ms=now_ms + ttl_ms,
                lane=lane if isinstance(lane, Lane) else Lane(str(lane)),
                provider=provider,
                book_id=book_id,
            )
            self._leases[shot_hash] = lease
            return lease

    async def get_lease(self, shot_hash: str) -> ShotLease | None:
        async with self._lock:
            return self._leases.get(shot_hash)

    async def extend(self, *, shot_hash: str, fence: int, now_ms: int, ttl_ms: int) -> ShotLease:
        async with self._lock:
            lease = self._leases.get(shot_hash)
            if lease is None:
                raise FenceViolationError(f"no lease to extend for shot {shot_hash}")
            if lease.fence != fence:
                raise FenceViolationError(
                    f"stale fence {fence} for shot {shot_hash} (current {lease.fence})"
                )
            extended = lease.model_copy(update={"expires_at_ms": now_ms + ttl_ms})
            self._leases[shot_hash] = extended
            return extended

    async def release(self, *, shot_hash: str, fence: int) -> bool:
        async with self._lock:
            lease = self._leases.get(shot_hash)
            if lease is None:
                return False
            if lease.fence != fence:
                raise FenceViolationError(
                    f"stale fence {fence} cannot release shot {shot_hash} (current {lease.fence})"
                )
            del self._leases[shot_hash]
            return True

    async def list_leases(self) -> list[ShotLease]:
        async with self._lock:
            return list(self._leases.values())

    async def reap_expired(self, *, now_ms: int) -> list[ShotLease]:
        async with self._lock:
            dead = [lease for lease in self._leases.values() if now_ms >= lease.expires_at_ms]
            for lease in dead:
                del self._leases[lease.shot_hash]
            return dead
