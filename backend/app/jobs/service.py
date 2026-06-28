"""``JobService`` — the facade that wires the framework into one runnable unit.

Bundles a :class:`~app.jobs.registry.JobRegistry`, a :class:`~app.jobs.store.JobStore`,
an optional :class:`~app.jobs.lease.LeaderElector` (leader election), a
:class:`~app.jobs.scheduler.JobScheduler`, and a :class:`~app.jobs.runner.JobWorker`,
sharing one :class:`~app.jobs.clock.Clock` and one resource bag. This is what an
entrypoint constructs: call :meth:`start` to spin the loops and :meth:`stop` to
tear them down.

It is deliberately *not* wired into the FastAPI composition root by default — the
framework is self-contained and exercised through this facade + the harness so it
can't collide with the nine other agents touching the shared files. A factory
(:func:`build_job_service`) shows exactly how an entrypoint *would* wire it from a
Redis client + a session factory, with the built-in maintenance jobs registered
and the target subsystems injected as resources (each job no-ops cleanly when its
dependency is absent).

Operational helpers (:meth:`run_now`, :meth:`pause`, :meth:`resume`,
:meth:`list_runs`, :meth:`dead_letters`, :meth:`stats`) make it the single handle
an admin surface or a CLI would talk to.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from typing import Any

from app.core.logging import get_logger
from app.jobs.clock import Clock, SystemClock
from app.jobs.dispatcher import Dispatcher
from app.jobs.lease import LeaderElector
from app.jobs.registry import JobRegistry
from app.jobs.runner import JobWorker
from app.jobs.scheduler import JobScheduler
from app.jobs.store import InMemoryJobStore, JobStore, StoreStats
from app.jobs.types import JobRun, JobRunStatus

logger = get_logger("app.jobs.service")


class JobService:
    """One handle owning the registry, store, scheduler, worker, and leader lease."""

    def __init__(
        self,
        *,
        registry: JobRegistry,
        store: JobStore | None = None,
        clock: Clock | None = None,
        elector: LeaderElector | None = None,
        resources: Mapping[str, Any] | None = None,
        worker_concurrency: int = 2,
        lease_seconds: float = 300.0,
        scheduler_poll_s: float = 1.0,
        worker_idle_s: float = 1.0,
        seed: int | None = None,
    ) -> None:
        self.registry = registry
        self.clock = clock or SystemClock()
        self.store: JobStore = store or InMemoryJobStore(clock=self.clock)
        self.elector = elector
        self._resources = dict(resources or {})
        self.dispatcher = Dispatcher(
            registry=registry,
            store=self.store,
            clock=self.clock,
            resources=self._resources,
            rng=random.Random(seed) if seed is not None else None,
        )
        self.scheduler = JobScheduler(
            registry=registry,
            store=self.store,
            clock=self.clock,
            is_leader=self._is_leader,
            poll_interval_s=scheduler_poll_s,
        )
        self.worker = JobWorker(
            store=self.store,
            dispatcher=self.dispatcher,
            clock=self.clock,
            concurrency=worker_concurrency,
            lease_seconds=lease_seconds,
            idle_sleep_s=worker_idle_s,
        )
        self._started = False

    def _is_leader(self) -> bool:
        # No elector => single-node mode (always leader). With an elector, gate
        # scheduling on actual leadership.
        return self.elector.is_leader if self.elector is not None else True

    # -- lifecycle ----------------------------------------------------------- #

    def start(self) -> None:
        """Spin up the leader elector (if any), scheduler, and worker loops."""
        if self._started:
            return
        if self.elector is not None:
            self.elector.start()
        self.scheduler.start()
        self.worker.start()
        self._started = True
        logger.info(
            "jobs.service.started",
            jobs=len(self.registry),
            leader_election=self.elector is not None,
        )

    async def stop(self) -> None:
        """Tear down the worker, scheduler, and elector (releases the lease)."""
        if not self._started:
            return
        await self.worker.stop()
        await self.scheduler.stop()
        if self.elector is not None:
            await self.elector.stop()
        self._started = False
        logger.info("jobs.service.stopped")

    # -- operations ---------------------------------------------------------- #

    async def run_now(
        self, job_name: str, *, payload: Mapping[str, Any] | None = None
    ) -> str | None:
        """Enqueue ``job_name`` for immediate execution (returns run id, or None if deduped)."""
        definition = self.registry.require(job_name)
        now = self.clock.now()
        key = definition.idempotency_key(now, payload)
        result = await self.store.enqueue(
            job_name=job_name,
            idempotency_key=key,
            scheduled_for=now,
            max_attempts=definition.max_attempts,
            trigger_kind=definition.trigger.kind,
            payload=payload,
            available_at=now,
        )
        return result.run.id if result.created else None

    def pause(self, job_name: str) -> None:
        """Stop auto-firing ``job_name``."""
        self.scheduler.pause(job_name)

    def resume(self, job_name: str) -> None:
        """Re-enable auto-firing ``job_name``."""
        self.scheduler.resume(job_name)

    def is_paused(self, job_name: str) -> bool:
        """Whether ``job_name`` is currently paused."""
        return self.scheduler.is_paused(job_name)

    async def list_runs(
        self, *, job_name: str | None = None, status: JobRunStatus | None = None, limit: int = 100
    ) -> list[JobRun]:
        """List run records (newest first), optionally filtered."""
        return await self.store.list_runs(job_name=job_name, status=status, limit=limit)

    async def dead_letters(self, *, limit: int = 100) -> list[JobRun]:
        """List dead-lettered runs (the framework's DLQ)."""
        return await self.store.dead_letters(limit=limit)

    async def replay(self, run_id: str) -> str | None:
        """Re-enqueue a dead-lettered/terminal run under a fresh active run.

        The DLQ record is preserved for audit; this creates a *new* run for the
        same logical work (same job + payload), so an operator can re-drive a job
        that failed after a fix. Returns the new run id (``None`` if a live run
        already exists for the key).
        """
        run = await self.store.get(run_id)
        if run is None:
            return None
        definition = self.registry.get(run.job_name)
        max_attempts = definition.max_attempts if definition is not None else run.max_attempts
        now = self.clock.now()
        result = await self.store.enqueue(
            job_name=run.job_name,
            idempotency_key=f"{run.idempotency_key}#replay@{now.isoformat()}",
            scheduled_for=now,
            max_attempts=max_attempts,
            trigger_kind=run.trigger_kind,
            payload=run.payload,
            available_at=now,
        )
        return result.run.id if result.created else None

    async def stats(self) -> StoreStats:
        """A snapshot of run counts + lifetime counters."""
        return await self.store.stats()

    @property
    def is_leader(self) -> bool:
        """Whether this node is currently the scheduling leader."""
        return self._is_leader()


def build_job_service(
    *,
    redis: Any | None = None,
    session_factory: Callable[[], Any] | None = None,
    registry: JobRegistry | None = None,
    resources: Mapping[str, Any] | None = None,
    enable_leader_election: bool = True,
    store_backend: str = "auto",
) -> JobService:
    """Factory: wire a :class:`JobService` for an entrypoint.

    ``store_backend`` selects the durable store: ``"redis"``, ``"postgres"``,
    ``"memory"``, or ``"auto"`` (postgres if a ``session_factory`` is given, else
    redis if a client is given, else in-memory). Leader election is enabled when a
    Redis client is available. This mirrors how the API/worker entrypoints build
    the render queue + scheduler, but stays out of the composition root by default.
    """
    reg = registry or JobRegistry()
    clock = SystemClock()

    store: JobStore
    if store_backend == "postgres" or (store_backend == "auto" and session_factory is not None):
        from app.jobs.db_store import PostgresJobStore

        if session_factory is None:
            raise ValueError("postgres store backend requires a session_factory")
        store = PostgresJobStore(session_factory)
    elif store_backend == "redis" or (store_backend == "auto" and redis is not None):
        from app.jobs.redis_store import RedisJobStore

        if redis is None:
            raise ValueError("redis store backend requires a redis client")
        store = RedisJobStore(redis)
    else:
        store = InMemoryJobStore(clock=clock)

    elector: LeaderElector | None = None
    if enable_leader_election and redis is not None:
        from app.jobs.lease import LeaderLease

        elector = LeaderElector(LeaderLease(redis), clock=clock)

    return JobService(
        registry=reg,
        store=store,
        clock=clock,
        elector=elector,
        resources=resources,
    )


__all__ = ["JobService", "build_job_service"]
