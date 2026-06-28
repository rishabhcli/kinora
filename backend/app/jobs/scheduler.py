"""The job scheduler — evaluate triggers under the leader lease, enqueue due runs.

The scheduler holds, per registered (non-manual) job, the *last fire time* it has
already enqueued. On each :meth:`tick` it asks each job's trigger for the next
fire instant and, when that instant is at or before *now*, enqueues a run for it
(the store dedups, so a tick that runs twice — or two scheduler nodes — produces
one logical run). It then records that fire time so the *next* tick computes the
following fire from it.

Crucially, the scheduler only enqueues when it is the **leader** (gated by an
injected ``is_leader`` callable, satisfied by :class:`~app.jobs.lease.LeaderElector`).
A follower's tick is a clean no-op. This is what makes a periodic job fire on
exactly one node even though every node runs the same loop.

State (per-job last-fire) lives in memory: it is an *optimization* to avoid
re-deriving fire times, not a correctness requirement — the store's idempotency
is the real guard, so a scheduler restart (which resets last-fire) at worst
re-enqueues an already-present run, which dedups. A persistent variant can hang
off the durable store later (left for the roadmap).

:meth:`run` drives the loop against the injected clock; tests call :meth:`tick`
directly for determinism.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.core.logging import get_logger
from app.jobs.clock import Clock, SystemClock
from app.jobs.registry import JobDefinition, JobRegistry
from app.jobs.store import JobStore
from app.jobs.types import ScheduledJobState

logger = get_logger("app.jobs.scheduler")


@dataclass(slots=True)
class _JobState:
    """The scheduler's in-memory bookkeeping for one registered job.

    ``epoch`` is the instant the scheduler first observed this job — the stable
    anchor an interval trigger computes its grid from, so the *first* fire of an
    unanchored interval job lands at ``epoch + interval`` (not "interval after
    whenever the loop happened to tick"). ``last_fire`` is the latest instant we
    have already enqueued a run for.
    """

    epoch: datetime | None = None
    last_fire: datetime | None = None
    state: ScheduledJobState = ScheduledJobState.ENABLED


class JobScheduler:
    """Enqueue due runs for the registry's scheduled jobs, when leader."""

    def __init__(
        self,
        *,
        registry: JobRegistry,
        store: JobStore,
        clock: Clock | None = None,
        is_leader: Callable[[], bool] | None = None,
        poll_interval_s: float = 1.0,
    ) -> None:
        self._registry = registry
        self._store = store
        self._clock = clock or SystemClock()
        self._is_leader = is_leader or (lambda: True)
        self._poll_interval_s = poll_interval_s
        self._state: dict[str, _JobState] = {}
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        # The scheduler's birth instant: the default anchor for a job's first
        # fire, so a job registered before the loop starts anchors to construction
        # time rather than to whichever later tick first noticed it.
        self._epoch = self._clock.now()

    def _job_state(self, definition: JobDefinition) -> _JobState:
        st = self._state.get(definition.name)
        if st is None:
            st = _JobState(state=definition.default_state)
            self._state[definition.name] = st
        return st

    def pause(self, name: str) -> None:
        """Stop auto-firing ``name`` until :meth:`resume` (idempotent)."""
        definition = self._registry.require(name)
        self._job_state(definition).state = ScheduledJobState.PAUSED

    def resume(self, name: str) -> None:
        """Re-enable auto-firing for ``name`` (idempotent)."""
        definition = self._registry.require(name)
        self._job_state(definition).state = ScheduledJobState.ENABLED

    def is_paused(self, name: str) -> bool:
        """Whether ``name`` is currently paused."""
        return self._job_state(self._registry.require(name)).state is ScheduledJobState.PAUSED

    async def tick(self, *, now: datetime | None = None) -> list[str]:
        """Evaluate every scheduled job once; return the ids of runs newly enqueued.

        A clean no-op (returns ``[]``) when not leader. The store dedups, so a
        re-run of a tick or a concurrent follower can never double-enqueue.
        """
        if not self._is_leader():
            return []
        moment = now or self._clock.now()
        enqueued: list[str] = []
        for definition in self._registry.scheduled():
            st = self._job_state(definition)
            if st.state is ScheduledJobState.PAUSED:
                continue
            run_id = await self._maybe_enqueue(definition, st, moment)
            if run_id is not None:
                enqueued.append(run_id)
        return enqueued

    async def _maybe_enqueue(
        self, definition: JobDefinition, st: _JobState, now: datetime
    ) -> str | None:
        # Anchor the first fire to the scheduler's epoch (its construction
        # instant), so an unanchored interval job fires at ``epoch + interval``
        # deterministically rather than "interval after whichever tick noticed it".
        if st.epoch is None:
            st.epoch = self._epoch
        anchor = st.last_fire if st.last_fire is not None else st.epoch
        try:
            fire = definition.trigger.next_fire(after=anchor, last_fire=st.last_fire)
        except Exception as exc:  # noqa: BLE001 - a bad trigger must not stall the loop
            logger.warning("jobs.scheduler.trigger_error", job=definition.name, error=str(exc))
            return None
        if fire is None or fire > now:
            return None

        key = definition.idempotency_key(fire)
        result = await self._store.enqueue(
            job_name=definition.name,
            idempotency_key=key,
            scheduled_for=fire,
            max_attempts=definition.max_attempts,
            trigger_kind=definition.trigger.kind,
            available_at=fire,
        )
        st.last_fire = fire
        if result.created:
            logger.info(
                "jobs.scheduler.enqueued",
                job=definition.name,
                run_id=result.run.id,
                scheduled_for=fire.isoformat(),
            )
            return result.run.id
        return None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.tick()
            await self._clock.sleep(self._poll_interval_s)

    def start(self) -> None:
        """Spawn the background scheduling loop (idempotent)."""
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the scheduling loop."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


__all__ = ["JobScheduler"]
