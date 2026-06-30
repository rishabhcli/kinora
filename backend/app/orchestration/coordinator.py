"""The render coordinator: assign queued shots to workers (kinora.md §12.1/§12.2).

This is the brain of distributed render orchestration. It owns the *assignment*
decision — given a set of queued :class:`ShotTicket`s and the live registry, it
shards each ticket to exactly one worker, honouring:

* **capability** — a worker only gets shots in lanes/providers it can serve;
* **provider capacity** — it never over-commits a throttled / budget-bound
  provider (via the :class:`CapacityOracle` seam);
* **locality** — a book sticks to one worker (the *book owner*) so its shots keep
  warm references + canon cache for visual continuity, until that owner is full
  or gone, at which point the book re-homes.

Exactly-once handoff is enforced by the lease + fence in the store: assignment
acquires a lease; only the holding worker (with the live fence) can extend or
complete it. A crashed worker's lease expires and is reclaimed by the registry
sweep, after which :meth:`assign` re-homes the orphaned shot to a healthy worker —
crash reassignment with no double-render.

The coordinator is pure coordination over the injected store / oracle / clock, so
the entire flow — assignment, contention, stealing, recovery, progress — runs on
the in-memory store under a virtual clock with fake workers and **zero infra**.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import structlog

from app.orchestration.capacity import CapacityOracle, _UnboundedCapacityOracle
from app.orchestration.clock import Clock
from app.orchestration.models import (
    Lane,
    ShotLease,
    ShotTicket,
)
from app.orchestration.placement import WorkerLoad, choose_worker
from app.orchestration.registry import WorkerRegistry
from app.orchestration.store import OrchestrationStore

logger = structlog.get_logger("app.orchestration.coordinator")

__all__ = ["Assignment", "AssignmentBatch", "RenderCoordinator"]


@dataclass(frozen=True, slots=True)
class Assignment:
    """One successful shot→worker handoff (a granted lease)."""

    ticket: ShotTicket
    lease: ShotLease

    @property
    def worker_id(self) -> str:
        return self.lease.worker_id


@dataclass(frozen=True, slots=True)
class AssignmentBatch:
    """The result of assigning a batch of tickets in one pass."""

    assigned: tuple[Assignment, ...]
    #: Tickets left unplaced this pass (no capable worker had room / capacity).
    deferred: tuple[ShotTicket, ...]

    @property
    def assigned_count(self) -> int:
        return len(self.assigned)


class RenderCoordinator:
    """Assigns queued shots to capable workers with locality + exactly-once leases."""

    def __init__(
        self,
        registry: WorkerRegistry,
        store: OrchestrationStore,
        *,
        clock: Clock,
        oracle: CapacityOracle | None = None,
        lease_ttl_ms: int | None = None,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock
        self._oracle = oracle or _UnboundedCapacityOracle()
        self._lease_ttl_ms = lease_ttl_ms or registry.config.lease_ttl_ms

    @property
    def oracle(self) -> CapacityOracle:
        return self._oracle

    @property
    def store(self) -> OrchestrationStore:
        """The backing store (handy for tests/inspection)."""
        return self._store

    # -- assignment ---------------------------------------------------------- #

    async def assign(self, tickets: Iterable[ShotTicket]) -> AssignmentBatch:
        """Place a batch of tickets onto workers in one coordinated pass.

        Committed shots are placed first (the buffer is sacred, §12.2), then
        speculative, then keyframe — so scarce slots favour the reader's path.
        Within a lane, locality is honoured: the first shot of a book establishes
        its owner; siblings follow. Load is tracked *incrementally* across the
        batch so two shots in one pass don't both target the same single free slot.
        """
        ordered = sorted(tickets, key=self._lane_rank)
        workers = await self._registry.assignable_workers()
        # Live load + book-ownership, mutated incrementally as we place in-batch.
        tracker = _LoadTracker.from_leases(await self._store.list_leases())

        assigned: list[Assignment] = []
        deferred: list[ShotTicket] = []

        for ticket in ordered:
            worker_id = choose_worker(
                ticket,
                workers,
                tracker.loads_for_book(ticket.book_id),
                oracle=self._oracle,
                sticky_book_owner=tracker.owner_of(ticket.book_id),
            )
            if worker_id is None:
                deferred.append(ticket)
                continue
            lease = await self._store.try_acquire(
                shot_hash=ticket.shot_hash,
                worker_id=worker_id,
                lane=ticket.lane,
                provider=ticket.provider,
                book_id=ticket.book_id,
                now_ms=self._clock.now_ms(),
                ttl_ms=self._lease_ttl_ms,
            )
            if lease is None:
                # Lost the race — another coordinator/worker already holds it. The
                # single-renderer invariant held; just skip (idempotent assign).
                deferred.append(ticket)
                continue
            self._oracle.note_assigned(ticket.provider, video_seconds=ticket.video_seconds)
            assigned.append(Assignment(ticket=ticket, lease=lease))
            # Reflect the new lease in the in-batch tracker so later tickets see it
            # (incremental, no re-query of the store per ticket).
            tracker.add(worker_id, ticket.book_id)

        if assigned:
            logger.info(
                "coordinator.assigned",
                count=len(assigned),
                deferred=len(deferred),
                workers=len(workers),
            )
        return AssignmentBatch(assigned=tuple(assigned), deferred=tuple(deferred))

    # -- lease lifecycle (worker-facing) ------------------------------------- #

    async def heartbeat_lease(self, lease: ShotLease) -> ShotLease:
        """Extend a held lease (worker still rendering). Fenced against zombies.

        Raises :class:`FenceViolationError` if the lease was reassigned out from under
        this worker — the worker must stop rendering and drop the result.
        """
        return await self._store.extend(
            shot_hash=lease.shot_hash,
            fence=lease.fence,
            now_ms=self._clock.now_ms(),
            ttl_ms=self._lease_ttl_ms,
        )

    async def complete(self, lease: ShotLease, *, video_seconds: float | None = None) -> bool:
        """Finish a shot: release the lease + return provider headroom. Fenced.

        ``video_seconds`` defaults to the lease's lane semantics is not tracked on
        the lease, so callers pass the actual spend; omitting it returns nothing to
        the oracle (safe for the keyframe lane which spent zero).
        """
        released = await self._store.release(shot_hash=lease.shot_hash, fence=lease.fence)
        if released:
            self._oracle.note_released(lease.provider, video_seconds=video_seconds or 0.0)
        return released

    # -- crash reassignment -------------------------------------------------- #

    async def reassign_orphans(self, orphans: Sequence[ShotTicket]) -> AssignmentBatch:
        """Re-home shots whose lease was reclaimed (dead worker / expiry).

        Identical to :meth:`assign` but named for intent: after a registry sweep
        returns reclaimed leases, the caller rebuilds the orphaned tickets and
        hands them here. The fence has already advanced on reclaim, so the old
        worker can never resurrect its claim.
        """
        return await self.assign(orphans)

    @staticmethod
    def _lane_rank(ticket: ShotTicket) -> int:
        """Sort key placing committed before speculative before keyframe."""
        order = {Lane.COMMITTED: 0, Lane.SPECULATIVE: 1, Lane.KEYFRAME: 2}
        return order.get(ticket.lane, 9)


class _LoadTracker:
    """Mutable per-worker / per-book lease counts derived from live leases.

    Built once per :meth:`RenderCoordinator.assign` pass from the store snapshot,
    then mutated in place as the pass places shots — so the coordinator never has
    to re-query the store per ticket, and two shots in one batch can't both be
    handed the same single free slot.
    """

    def __init__(self) -> None:
        self._held: dict[str, int] = {}
        self._per_book: dict[str, dict[str, int]] = {}

    @classmethod
    def from_leases(cls, leases: Sequence[ShotLease]) -> _LoadTracker:
        tracker = cls()
        for lease in leases:
            tracker.add(lease.worker_id, lease.book_id)
        return tracker

    def add(self, worker_id: str, book_id: str) -> None:
        self._held[worker_id] = self._held.get(worker_id, 0) + 1
        book_counts = self._per_book.setdefault(worker_id, {})
        book_counts[book_id] = book_counts.get(book_id, 0) + 1

    def loads_for_book(self, book_id: str) -> dict[str, WorkerLoad]:
        """A :class:`WorkerLoad` per worker, with the per-book count for ``book_id``."""
        return {
            worker_id: WorkerLoad(
                worker_id=worker_id,
                leases_held=held,
                leases_for_book=self._per_book.get(worker_id, {}).get(book_id, 0),
            )
            for worker_id, held in self._held.items()
        }

    def owner_of(self, book_id: str) -> str | None:
        """The worker holding the most shots of ``book_id`` (the locality anchor).

        Ties resolve to the lexicographically smallest worker_id for determinism.
        ``None`` if no worker currently holds any shot of the book.
        """
        holders = {
            worker_id: counts[book_id]
            for worker_id, counts in self._per_book.items()
            if counts.get(book_id, 0) > 0
        }
        if not holders:
            return None
        return max(holders.items(), key=lambda kv: (kv[1], _neg_lex_key(kv[0])))[0]


def _neg_lex_key(worker_id: str) -> tuple[int, ...]:
    return tuple(-ord(ch) for ch in worker_id)
