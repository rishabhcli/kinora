"""Drain + graceful shutdown coordination with the queue/render workers.

When a rollout retires an old version of the **render-worker** role
(``ServiceRole.RENDER_WORKER``), that instance is mid-flight: it may be polling
the §12.1 Redis priority queue and holding leases on in-flight shots. Killing it
abruptly orphans those leases until the reaper expires them and risks a
half-written clip. The orchestrator therefore *drains* the worker before
shutdown, in three phases that mirror how ``app.queue.worker.RenderWorker``
actually stops (set a ``stop`` event → lane loops finish the current job → the
process exits):

1. **Cordon** — stop accepting *new* lane work (the worker's ``stop`` event is
   set; lane loops will not claim another job after the current one).
2. **Quiesce** — wait for in-flight jobs to finish, up to a deadline, polling
   the worker's in-flight count. Speculative jobs may be cancelled immediately
   (§4.8 cancellation token); committed jobs are allowed to finish.
3. **Terminate** — once in-flight reaches 0 (or the deadline passes), signal the
   process to exit. Any jobs still in flight at the deadline are *released* back
   to the queue (their lease is dropped) so a surviving worker re-claims them —
   the film never loses a shot.

The worker is reached through a tiny :class:`DrainTarget` Protocol so this logic
is testable against a fake worker with no Redis. The render-worker side already
supports cooperative stop via its ``asyncio.Event`` (see ``RenderWorker.run``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class DrainPhase(StrEnum):
    RUNNING = "running"
    CORDONED = "cordoned"
    QUIESCING = "quiescing"
    DRAINED = "drained"
    TERMINATED = "terminated"
    TIMED_OUT = "timed-out"


@runtime_checkable
class DrainTarget(Protocol):
    """A worker instance that can be cooperatively drained.

    Maps onto ``app.queue.worker.RenderWorker``: ``cordon`` sets the stop event,
    ``inflight`` reports the number of jobs still being processed, ``release``
    drops a job's lease back to the queue, and ``terminate`` exits the process.
    """

    async def cordon(self) -> None:
        """Stop claiming new jobs (set the worker's stop event)."""
        ...

    async def inflight(self) -> int:
        """Number of jobs still being processed by this instance."""
        ...

    async def release_inflight(self) -> int:
        """Release all still-in-flight job leases back to the queue.

        Returns the count released. Called only at the drain deadline so a
        surviving worker re-claims them.
        """
        ...

    async def terminate(self) -> None:
        """Exit the process (after drain or at the deadline)."""
        ...


@dataclass(frozen=True, slots=True)
class DrainResult:
    phase: DrainPhase
    inflight_at_start: int
    released: int
    polls: int
    elapsed: float

    @property
    def clean(self) -> bool:
        """True iff the worker fully quiesced before the deadline (no releases)."""
        return self.phase is DrainPhase.TERMINATED and self.released == 0


@dataclass(slots=True)
class DrainCoordinator:
    """Drives a :class:`DrainTarget` through cordon → quiesce → terminate.

    Time is injected (``now``) and pacing is owned by the caller (no real
    ``sleep``); each ``poll`` call reads the in-flight count once. The deadline
    is ``deadline_s`` of *virtual* time after cordon. ``grace_polls`` caps the
    number of quiesce polls so a stuck worker can't loop forever even if the
    clock never advances in a test.
    """

    target: DrainTarget
    now: Callable[[], float]
    deadline_s: float = 90.0
    grace_polls: int = 1000
    phase: DrainPhase = field(default=DrainPhase.RUNNING, init=False)

    async def drain(self) -> DrainResult:
        start = self.now()
        inflight_start = await self.target.inflight()

        await self.target.cordon()
        self.phase = DrainPhase.CORDONED

        self.phase = DrainPhase.QUIESCING
        polls = 0
        released = 0
        while True:
            remaining = await self.target.inflight()
            polls += 1
            if remaining <= 0:
                self.phase = DrainPhase.DRAINED
                break
            elapsed = self.now() - start
            if elapsed >= self.deadline_s or polls >= self.grace_polls:
                released = await self.target.release_inflight()
                self.phase = DrainPhase.TIMED_OUT
                break

        await self.target.terminate()
        elapsed = self.now() - start
        # A timed-out drain still terminates the process, but we keep the
        # TIMED_OUT phase visible so the audit trail records that jobs were
        # released rather than finished. A clean drain becomes TERMINATED.
        final = DrainPhase.TERMINATED if self.phase is DrainPhase.DRAINED else DrainPhase.TIMED_OUT
        self.phase = final
        return DrainResult(
            phase=final,
            inflight_at_start=inflight_start,
            released=released,
            polls=polls,
            elapsed=elapsed,
        )
