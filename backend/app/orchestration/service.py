"""The orchestration service façade + control-loop tick (kinora.md §12.1/§12.2).

:class:`OrchestrationService` wires the registry, coordinator, and rebalancer into
the single object a render-orchestrator process drives. Its :meth:`tick` is one
pass of the control loop:

1. **sweep** dead workers + expired leases (crash recovery), collecting orphaned
   shots to re-home;
2. **assign** the pending tickets (orphans first, then newly queued), honouring
   capability / provider-capacity / locality;
3. **rebalance** — plan work-stealing migrations from backed-up to idle workers and
   apply them (re-acquiring leases against the new worker, which advances the fence
   and fences the old worker out — the same exactly-once handoff as recovery);
4. project a :class:`FleetProgress` snapshot for observability.

The service performs no provider I/O and reads time from the injected clock, so
the whole loop is driven deterministically in tests with a virtual clock + fake
workers + the in-memory store. A ticket source is injected as a callable (a real
deployment hands it a peek at the Redis queue); the orchestrator never owns the
render payload — it only decides *who* renders *what*.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import structlog

from app.orchestration.capacity import CapacityOracle
from app.orchestration.clock import Clock
from app.orchestration.coordinator import AssignmentBatch, RenderCoordinator
from app.orchestration.models import ShotLease, ShotTicket
from app.orchestration.progress import FleetProgress, build_progress
from app.orchestration.rebalance import Migration, Rebalancer, StealPlan
from app.orchestration.registry import SweepReport, WorkerRegistry
from app.orchestration.store import OrchestrationStore

logger = structlog.get_logger("app.orchestration.service")

__all__ = [
    "TickReport",
    "TicketSource",
    "OrchestrationService",
    "build_orchestration_service",
]

#: Resolves the orchestrator's view of currently-queued shots awaiting placement.
#: A real deployment peeks the Redis queue; tests pass a list/closure. Tickets
#: already leased are filtered out by the service so a re-peek is harmless.
TicketSource = Callable[[], Awaitable[Sequence[ShotTicket]]] | Callable[[], Sequence[ShotTicket]]


@dataclass(frozen=True, slots=True)
class TickReport:
    """The outcome of one control-loop pass — everything a metrics sink wants."""

    sweep: SweepReport
    assignment: AssignmentBatch
    steal_plan: StealPlan
    stolen: tuple[Migration, ...]
    progress: FleetProgress

    @property
    def did_recover(self) -> bool:
        return self.sweep.any_recovery


class OrchestrationService:
    """Drives the distributed render fleet: register, assign, steal, recover."""

    def __init__(
        self,
        *,
        registry: WorkerRegistry,
        coordinator: RenderCoordinator,
        rebalancer: Rebalancer,
        store: OrchestrationStore,
        clock: Clock,
        ticket_source: TicketSource | None = None,
    ) -> None:
        self._registry = registry
        self._coordinator = coordinator
        self._rebalancer = rebalancer
        self._store = store
        self._clock = clock
        self._ticket_source = ticket_source

    @property
    def registry(self) -> WorkerRegistry:
        return self._registry

    @property
    def coordinator(self) -> RenderCoordinator:
        return self._coordinator

    # -- the control loop ---------------------------------------------------- #

    async def tick(self, tickets: Sequence[ShotTicket] | None = None) -> TickReport:
        """Run one coordination pass and return a full :class:`TickReport`.

        ``tickets`` overrides the injected source for this pass (handy in tests).
        Orphaned shots reclaimed by the sweep are re-homed first, ahead of newly
        queued work, so a crash never strands a committed shot behind fresh
        speculative ones.
        """
        # 1. Crash recovery: reclaim dead-worker / expired leases.
        sweep = await self._registry.sweep()
        orphan_tickets = self._orphans_to_tickets(sweep.reclaimed_leases, await self._pending())

        # 2. Assignment: orphans first, then everything still queued + unleased.
        pending = await self._pending() if tickets is None else list(tickets)
        unleased = await self._filter_unleased(pending)
        to_place = self._dedup_by_hash([*orphan_tickets, *unleased])
        assignment = await self._coordinator.assign(to_place)

        # 3. Rebalance: steal work from backed-up workers onto idle ones.
        workers = await self._registry.assignable_workers()
        leases = await self._store.list_leases()
        steal_plan = self._rebalancer.plan(workers, leases)
        stolen = await self._apply_steals(steal_plan)

        # 4. Progress snapshot for observability.
        progress = await self.snapshot(queued=unleased)

        if assignment.assigned or stolen or sweep.any_recovery:
            logger.info(
                "orchestration.tick",
                assigned=assignment.assigned_count,
                deferred=len(assignment.deferred),
                stolen=len(stolen),
                reclaimed=len(sweep.reclaimed_leases),
                dead_workers=len(sweep.dead_workers),
            )
        return TickReport(
            sweep=sweep,
            assignment=assignment,
            steal_plan=steal_plan,
            stolen=tuple(stolen),
            progress=progress,
        )

    async def snapshot(self, *, queued: Sequence[ShotTicket] = ()) -> FleetProgress:
        """Build a :class:`FleetProgress` from the current registry + lease state."""
        return build_progress(
            await self._store.list_workers(),
            await self._store.list_leases(),
            queued,
            now_ms=self._clock.now_ms(),
            worker_ttl_ms=self._registry.config.worker_ttl_ms,
        )

    # -- work-stealing application ------------------------------------------- #

    async def _apply_steals(self, plan: StealPlan) -> list[Migration]:
        """Apply a steal plan: re-acquire each migrated lease against its new worker.

        A migration is *atomic via the fence*: ``try_acquire`` for the new worker
        only succeeds once the prior lease is released, and it advances the fence so
        the old worker is fenced out of any in-flight heartbeat. We release the old
        lease (at its current fence) then acquire for the new worker; if the old
        lease vanished underneath us (a racing sweep), we skip — the shot is already
        free and the next assign pass will place it.
        """
        if plan.is_empty:
            return []
        applied: list[Migration] = []
        now = self._clock.now_ms()
        ttl = self._registry.config.lease_ttl_ms
        for migration in plan.migrations:
            current = await self._store.get_lease(migration.shot_hash)
            if current is None or current.worker_id != migration.from_worker:
                continue  # already moved/freed — skip
            try:
                released = await self._store.release(
                    shot_hash=migration.shot_hash, fence=current.fence
                )
            except Exception as exc:  # noqa: BLE001 - a racing writer beat us
                logger.info(
                    "orchestration.steal_release_skip", shot=migration.shot_hash, err=str(exc)
                )
                continue
            if not released:
                continue
            lease = await self._store.try_acquire(
                shot_hash=migration.shot_hash,
                worker_id=migration.to_worker,
                lane=migration.lane,
                provider=current.provider,
                book_id=migration.book_id,
                now_ms=now,
                ttl_ms=ttl,
            )
            if lease is not None:
                applied.append(migration)
        if applied:
            logger.info("orchestration.stole", count=len(applied))
        return applied

    # -- helpers ------------------------------------------------------------- #

    async def _pending(self) -> list[ShotTicket]:
        """Resolve the injected ticket source (sync or async), or empty."""
        if self._ticket_source is None:
            return []
        result = self._ticket_source()
        if isinstance(result, Awaitable):
            result = await result
        return list(result)

    async def _filter_unleased(self, tickets: Sequence[ShotTicket]) -> list[ShotTicket]:
        """Drop tickets whose shot already holds a live lease (idempotent placement)."""
        now = self._clock.now_ms()
        out: list[ShotTicket] = []
        for ticket in tickets:
            lease = await self._store.get_lease(ticket.shot_hash)
            if lease is not None and now < lease.expires_at_ms:
                continue
            out.append(ticket)
        return out

    @staticmethod
    def _orphans_to_tickets(
        reclaimed: Sequence[ShotLease], pending: Sequence[ShotTicket]
    ) -> list[ShotTicket]:
        """Rebuild tickets for reclaimed leases, enriching from the pending pool.

        A reclaimed lease knows the shot's hash / lane / provider / book; if the
        same shot is also in the pending pool we keep that ticket's richer fields
        (video_seconds, scene/session) — otherwise we synthesise a minimal ticket
        so the orphan is still re-homed.
        """
        by_hash = {t.shot_hash: t for t in pending}
        tickets: list[ShotTicket] = []
        for lease in reclaimed:
            existing = by_hash.get(lease.shot_hash)
            if existing is not None:
                tickets.append(existing)
                continue
            tickets.append(
                ShotTicket(
                    shot_hash=lease.shot_hash,
                    book_id=lease.book_id,
                    lane=lease.lane,
                    provider=lease.provider,
                )
            )
        return tickets

    @staticmethod
    def _dedup_by_hash(tickets: Sequence[ShotTicket]) -> list[ShotTicket]:
        """First-wins de-dup by shot_hash (orphans precede pending, so they win)."""
        seen: set[str] = set()
        out: list[ShotTicket] = []
        for ticket in tickets:
            if ticket.shot_hash in seen:
                continue
            seen.add(ticket.shot_hash)
            out.append(ticket)
        return out


def build_orchestration_service(
    *,
    clock: Clock,
    store: OrchestrationStore | None = None,
    oracle: CapacityOracle | None = None,
    ticket_source: TicketSource | None = None,
    settings: object | None = None,
) -> OrchestrationService:
    """Wire an :class:`OrchestrationService` over the in-memory store by default.

    Reads timing knobs from ``settings`` when supplied (the app :class:`Settings`),
    else uses the registry/rebalancer defaults. The default store is in-memory so
    the service is constructible with zero infra; a production wiring passes a
    Redis-backed :class:`OrchestrationStore` and the real capacity oracle.
    """
    from app.orchestration.rebalance import RebalanceConfig
    from app.orchestration.registry import RegistryConfig
    from app.orchestration.store import InMemoryOrchestrationStore

    store = store or InMemoryOrchestrationStore(clock)

    reg_cfg = RegistryConfig()
    reb_cfg = RebalanceConfig()
    if settings is not None:
        reg_cfg = RegistryConfig(
            worker_ttl_ms=int(
                getattr(settings, "orchestration_worker_ttl_ms", reg_cfg.worker_ttl_ms)
            ),
            lease_ttl_ms=int(
                getattr(settings, "orchestration_lease_ttl_ms", reg_cfg.lease_ttl_ms)
            ),
        )
        reb_cfg = RebalanceConfig(
            imbalance_threshold=int(
                getattr(settings, "orchestration_rebalance_imbalance", reb_cfg.imbalance_threshold)
            ),
            max_steals=int(
                getattr(settings, "orchestration_rebalance_max_steals", reb_cfg.max_steals)
            ),
            steal_committed=bool(
                getattr(settings, "orchestration_steal_committed", reb_cfg.steal_committed)
            ),
        )

    registry = WorkerRegistry(store, clock=clock, config=reg_cfg)
    coordinator = RenderCoordinator(
        registry, store, clock=clock, oracle=oracle, lease_ttl_ms=reg_cfg.lease_ttl_ms
    )
    rebalancer = Rebalancer(reb_cfg)
    return OrchestrationService(
        registry=registry,
        coordinator=coordinator,
        rebalancer=rebalancer,
        store=store,
        clock=clock,
        ticket_source=ticket_source,
    )
