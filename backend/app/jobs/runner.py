"""The job worker — drain due runs from the store and dispatch them.

A worker repeatedly claims the next due run (the store's atomic lease) and hands
it to the :class:`~app.jobs.dispatcher.Dispatcher`. Unlike the scheduler, a worker
needs **no lease**: the store's claim is the exclusion, so every node can run
workers and they share the load (whoever claims first runs it). A worker also
periodically reaps expired leases so a crashed worker's in-flight run is recovered.

Concurrency is bounded by a pool of ``concurrency`` claim-and-dispatch coroutines
sharing one store; each loops claim → dispatch → repeat. When no run is due a
worker backs off by ``idle_sleep_s`` (virtual-clock friendly). :meth:`drain` runs
the pool until the store has no claimable work (used by the test harness and
one-shot batch processing); :meth:`run` runs forever until stopped.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime

from app.core.logging import get_logger
from app.jobs.clock import Clock, SystemClock
from app.jobs.dispatcher import Dispatcher, DispatchResult
from app.jobs.store import JobStore

logger = get_logger("app.jobs.runner")


class JobWorker:
    """Claim-and-dispatch loop(s) draining a :class:`JobStore`."""

    def __init__(
        self,
        *,
        store: JobStore,
        dispatcher: Dispatcher,
        clock: Clock | None = None,
        concurrency: int = 2,
        lease_seconds: float = 300.0,
        idle_sleep_s: float = 1.0,
        reap_interval_s: float = 30.0,
        job_names: list[str] | None = None,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self._store = store
        self._dispatcher = dispatcher
        self._clock = clock or SystemClock()
        self._concurrency = concurrency
        self._lease_seconds = lease_seconds
        self._idle_sleep_s = idle_sleep_s
        self._reap_interval_s = reap_interval_s
        self._job_names = job_names
        self._tasks: list[asyncio.Task[None]] = []
        self._reaper: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def claim_one(self, *, now: datetime | None = None) -> DispatchResult | None:
        """Claim and dispatch a single due run; ``None`` if nothing is due."""
        moment = now if now is not None else self._clock.now()
        run = await self._store.claim_due(
            now=moment, lease_seconds=self._lease_seconds, job_names=self._job_names
        )
        if run is None:
            return None
        return await self._dispatcher.dispatch(run)

    async def drain(self, *, max_runs: int | None = None) -> list[DispatchResult]:
        """Dispatch every currently-claimable run, oldest first; return the results.

        Deterministic single-pass drain (no sleeping) — the harness calls this
        after advancing the virtual clock so every now-due run executes. ``max_runs``
        caps the batch (a safety valve against an infinite retry-now loop).
        """
        results: list[DispatchResult] = []
        while max_runs is None or len(results) < max_runs:
            result = await self.claim_one()
            if result is None:
                break
            results.append(result)
        return results

    async def reap(self) -> int:
        """Re-queue runs whose worker lease has expired (crash recovery)."""
        return await self._store.reap_expired(now=self._clock.now())

    async def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                result = await self.claim_one()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                logger.error("jobs.worker.claim_error", error=str(exc))
                result = None
            if result is None:
                await self._clock.sleep(self._idle_sleep_s)

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                reaped = await self.reap()
                if reaped:
                    logger.info("jobs.worker.reaped", count=reaped)
            await self._clock.sleep(self._reap_interval_s)

    def start(self) -> None:
        """Spawn the worker pool + reaper (idempotent)."""
        if self._tasks:
            return
        self._stop = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._worker_loop()) for _ in range(self._concurrency)
        ]
        self._reaper = asyncio.create_task(self._reaper_loop())

    async def stop(self) -> None:
        """Stop the pool + reaper."""
        self._stop.set()
        tasks = [*self._tasks]
        if self._reaper is not None:
            tasks.append(self._reaper)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks = []
        self._reaper = None


__all__ = ["JobWorker"]
