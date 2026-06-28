"""Lease / visibility-timeout helpers + a standalone reaper (kinora.md §12.1).

A claimed render holds a **lease** (its entry in the queue's ``processing`` sorted
set, scored by an expiry deadline). The lease is the visibility timeout: while it
is held the job is invisible to other workers; once it lapses the reaper re-queues
it (crash recovery). The lease *must* outlive the whole render window or a slow
render gets reaped + re-claimed mid-flight, double-submitting it and (under live
video) double-spending the budget — so the worker heartbeats the lease while it
renders (§12.1).

The queue already exposes ``extend_lease`` / ``reap_expired`` / ``lease_ms``; this
module packages two production conveniences on top, both decoupled from the
worker so they are independently testable against the in-process fake:

* :class:`LeaseGuard` — an async context manager that heartbeats one job's lease
  on a fixed cadence for the duration of a ``with`` block, then stops cleanly.
  It is the same heartbeat the worker runs inline, extracted so any code path that
  holds a job for a while (a long QA pass, a degrade ladder) can borrow it.
* :class:`Reaper` — a small loop that periodically calls ``reap_expired`` (and
  refreshes the queue-depth gauges), for a dedicated recovery process or a test.

Neither calls a provider; both are pure queue/Redis orchestration.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.queue.leases")

__all__ = ["LeaseGuard", "Reaper"]


class LeaseGuard:
    """Heartbeat one job's lease on a cadence for the lifetime of a ``with`` block.

    ::

        async with LeaseGuard(queue, job_id, heartbeat_s=30):
            await long_running_render()  # lease is renewed every 30s

    The heartbeat cadence must be comfortably under the queue's ``lease_ms`` so a
    single missed beat never lets the reaper steal the job. A heartbeat after the
    job is no longer leased (acked/cancelled) is a harmless no-op, so leaving the
    block after completion is safe.
    """

    def __init__(
        self,
        queue: Any,
        job_id: str,
        *,
        heartbeat_s: float = 30.0,
    ) -> None:
        self._queue = queue
        self._job_id = job_id
        self._heartbeat_s = heartbeat_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self.beats = 0

    async def __aenter__(self) -> LeaseGuard:
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._heartbeat_s)
                return  # stop was set during the wait
            except TimeoutError:
                pass
            if self._stop.is_set():
                return
            with contextlib.suppress(Exception):
                if await self._queue.extend_lease(self._job_id):
                    self.beats += 1


class Reaper:
    """A periodic crash-recovery loop: reap expired leases + refresh depth gauges.

    Runs ``reap_expired`` every ``interval_s`` until stopped. Intended for a
    dedicated recovery process, but :meth:`run_once` is exposed for tests so the
    reap logic can be driven deterministically without a loop.
    """

    def __init__(self, queue: Any, *, interval_s: float = 5.0) -> None:
        self._queue = queue
        self._interval_s = interval_s
        self.total_reaped = 0

    async def run_once(self, *, now_ms: int | None = None) -> int:
        """One reap pass. Returns the number of leases reclaimed."""
        reaped = await self._queue.reap_expired(now_ms=now_ms)
        # Refresh the live queue-depth gauges off the same cadence (side effect of
        # stats()); guarded so a metrics hiccup never breaks recovery.
        with contextlib.suppress(Exception):
            await self._queue.stats()
        self.total_reaped += reaped
        if reaped:
            logger.info("reaper.reaped", count=reaped, total=self.total_reaped)
        return reaped

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        """Run the reap loop until ``stop`` is set."""
        stop = stop or asyncio.Event()
        while not stop.is_set():
            with contextlib.suppress(Exception):
                await self.run_once()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
