"""The saga worker loop — claim, drive, lease-renew, reap.

A fleet of :class:`SagaWorker`\\ s drains the durable :class:`SagaStore`: each
worker repeatedly *claims* the next runnable instance (leasing it so no peer
double-drives it), hands it to the :class:`SagaOrchestrator` to advance to a
terminal/parked state, and renews/releases the lease around that work. A separate
reap pass returns leased-but-lapsed instances to the runnable pool — which is how
a crashed worker's in-flight saga is picked up and **resumed** by a healthy one.

The loop is driven against an injected :class:`~app.jobs.clock.Clock`, so under
the manual clock a test advances virtual time and ``tick()`` makes exactly the
expected progress with no real sleeping. The design mirrors
:class:`app.jobs.dispatcher`/``worker`` so operators see one shape across the
jobs framework and the saga engine.
"""

from __future__ import annotations

import asyncio
import contextlib

import structlog

from app.distributed.sagas.orchestrator import SagaOrchestrator
from app.distributed.sagas.store import SagaStore
from app.distributed.sagas.types import SagaInstance
from app.jobs.clock import Clock, SystemClock

_log = structlog.get_logger(__name__)


class SagaWorker:
    """Claim → drive → reap loop over a :class:`SagaStore`.

    ``lease_seconds`` is how long a claimed instance is leased while a worker
    drives it; the orchestrator's drive of a single instance should comfortably
    fit (steps that sleep on backoff release the loop but keep the lease, so set
    the lease longer than the longest single backoff). ``reap_every_ticks`` runs
    the expired-lease reaper every N idle ticks so a crashed worker's saga is
    resumed promptly.
    """

    def __init__(
        self,
        store: SagaStore,
        orchestrator: SagaOrchestrator,
        *,
        clock: Clock | None = None,
        lease_seconds: float = 60.0,
        poll_interval_s: float = 1.0,
        definitions: list[str] | None = None,
    ) -> None:
        self._store = store
        self._orch = orchestrator
        self._clock = clock or SystemClock()
        self._lease_seconds = lease_seconds
        self._poll_interval_s = poll_interval_s
        self._definitions = definitions
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def claim_one(self) -> SagaInstance | None:
        """Claim the next runnable instance (leasing it), or ``None`` if idle."""
        return await self._store.claim_due(
            now=self._clock.now(),
            lease_seconds=self._lease_seconds,
            definitions=self._definitions,
        )

    async def tick(self) -> bool:
        """Claim and drive at most one instance. Returns whether work was done.

        Returns ``True`` if an instance was claimed and driven, ``False`` if the
        store was idle (the caller then sleeps a poll interval). Drives the claimed
        instance to whatever state the orchestrator reaches in this pass — for a
        saga with backoff sleeps, that is terminal (the orchestrator awaits the
        clock internally and the manual clock resolves it within the same tick).
        """
        instance = await self.claim_one()
        if instance is None:
            return False
        try:
            await self._orch.resume(instance.id)
        except Exception:  # noqa: BLE001 - never let one bad saga kill the loop
            _log.exception("saga drive failed", saga_id=instance.id)
        finally:
            await self._release(instance.id)
        return True

    async def _release(self, saga_id: str) -> None:
        # Clearing the lease lets the reaper / next claim see the instance again if
        # it is still active (parked on backoff); terminal instances are inert.
        inst = await self._store.get(saga_id)
        if inst is not None and not inst.is_terminal:
            inst.lease_token = None
            inst.lease_until = None
            await self._store.save_instance(inst)

    async def reap(self) -> int:
        """Return leased-but-lapsed instances to the runnable pool (crash recovery)."""
        return await self._store.reap_expired(now=self._clock.now())

    async def run_until_idle(self, *, max_iterations: int = 10_000) -> int:
        """Drive instances until the store is idle. Returns how many were driven.

        The synchronous workhorse for tests + one-shot drains: it keeps ticking
        (reaping between idle passes) until no instance is claimable, bounded by
        ``max_iterations`` as a safety net against a misbehaving saga.
        """
        driven = 0
        for _ in range(max_iterations):
            did_work = await self.tick()
            if did_work:
                driven += 1
                continue
            # Idle: reap any lapsed leases and try once more before giving up.
            if await self.reap() == 0:
                break
        return driven

    async def _loop(self) -> None:
        idle_ticks = 0
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                did_work = await self.tick()
                if did_work:
                    idle_ticks = 0
                    continue
                idle_ticks += 1
                if idle_ticks % 5 == 0:
                    await self.reap()
            await self._clock.sleep(self._poll_interval_s)

    def start(self) -> None:
        """Spawn the background drain loop (idempotent)."""
        if self._task is None or self._task.done():
            self._stop = asyncio.Event()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop (does not abort an in-flight drive's saga state)."""
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = None


__all__ = ["SagaWorker"]
