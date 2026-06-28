"""Deterministic virtual-clock test harness for the jobs framework.

Wires a :class:`~app.jobs.registry.JobRegistry`, an in-memory (or supplied)
:class:`~app.jobs.store.JobStore`, a :class:`~app.jobs.scheduler.JobScheduler`,
and a :class:`~app.jobs.runner.JobWorker` around a single
:class:`~app.jobs.clock.ManualClock`, then lets a test drive them **one virtual
tick at a time** — no real sleeping, no wall-clock flakiness:

    h = VirtualClockHarness(registry)
    await h.advance(60)          # move virtual time forward 60s
    results = await h.run_pending()   # scheduler tick + drain the worker

Because the whole stack shares the manual clock, an interval job set to "every
30s" fires exactly twice when you advance 60s, retries land at exactly their
backoff instants, and lease expiry is reproducible. The harness defaults to a
trivial always-leader so scheduling is exercised; pass ``is_leader`` to test the
follower path.

This is the headline deliverable for *testability*: the entire framework is
exercisable end-to-end with zero infrastructure and total determinism.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from app.jobs.clock import ManualClock
from app.jobs.dispatcher import Dispatcher, DispatchResult
from app.jobs.registry import JobRegistry
from app.jobs.runner import JobWorker
from app.jobs.scheduler import JobScheduler
from app.jobs.store import InMemoryJobStore, JobStore


class VirtualClockHarness:
    """A self-contained scheduler + worker + store on one advanceable clock."""

    def __init__(
        self,
        registry: JobRegistry,
        *,
        store: JobStore | None = None,
        start: datetime | None = None,
        resources: Mapping[str, Any] | None = None,
        is_leader: Callable[[], bool] | None = None,
        lease_seconds: float = 300.0,
        seed: int = 0,
    ) -> None:
        self.clock = ManualClock(start=start)
        self.registry = registry
        self.store: JobStore = store or InMemoryJobStore(clock=self.clock)
        self.dispatcher = Dispatcher(
            registry=registry,
            store=self.store,
            clock=self.clock,
            resources=resources or {},
            rng=random.Random(seed),
        )
        self.scheduler = JobScheduler(
            registry=registry,
            store=self.store,
            clock=self.clock,
            is_leader=is_leader or (lambda: True),
        )
        self.worker = JobWorker(
            store=self.store,
            dispatcher=self.dispatcher,
            clock=self.clock,
            lease_seconds=lease_seconds,
        )

    @property
    def now(self) -> datetime:
        """Current virtual instant."""
        return self.clock.now()

    async def advance(self, seconds: float) -> None:
        """Move virtual time forward by ``seconds`` (releases due sleepers)."""
        await self.clock.advance(seconds)

    async def advance_to(self, instant: datetime) -> None:
        """Advance virtual time to an absolute UTC ``instant``."""
        await self.clock.advance_to(instant)

    async def tick_scheduler(self) -> list[str]:
        """Run one scheduler evaluation; return ids of newly-enqueued runs."""
        return await self.scheduler.tick()

    async def drain_worker(self, *, max_runs: int | None = None) -> list[DispatchResult]:
        """Dispatch every currently-claimable run."""
        return await self.worker.drain(max_runs=max_runs)

    async def run_pending(self, *, max_runs: int | None = None) -> list[DispatchResult]:
        """One full cycle: scheduler tick, then drain the worker. The common case."""
        await self.scheduler.tick()
        return await self.worker.drain(max_runs=max_runs)

    async def reap(self) -> int:
        """Re-queue runs whose lease expired at the current virtual instant."""
        return await self.store.reap_expired(now=self.now)

    async def run_now(
        self, job_name: str, *, payload: Mapping[str, Any] | None = None
    ) -> str | None:
        """Force-enqueue ``job_name`` immediately (manual trigger / ad-hoc run).

        Returns the run id (``None`` if a dedup collapsed it into an existing
        active run). Use this to exercise manual jobs or to inject a one-off.
        """
        definition = self.registry.require(job_name)
        key = definition.idempotency_key(self.now, payload)
        result = await self.store.enqueue(
            job_name=job_name,
            idempotency_key=key,
            scheduled_for=self.now,
            max_attempts=definition.max_attempts,
            trigger_kind=definition.trigger.kind,
            payload=payload,
            available_at=self.now,
        )
        return result.run.id if result.created else None


__all__ = ["VirtualClockHarness"]
