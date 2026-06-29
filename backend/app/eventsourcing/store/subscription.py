"""Catch-up subscriptions — the projection facet's read-side primitive.

A :class:`CatchUpSubscription` reads the global log forward from a durable
:class:`~contracts.Checkpoint`, hands each event to a handler, and advances the
checkpoint. It is the building block for read models / projections (§6 data
plane) and for the §12.5 "buffer-occupancy / event timeline" style observers.

Design:

* **Gap-free resume.** Because :meth:`EventStore.read_all` returns dense,
  gap-free positions, the subscription can record "processed ≤ position" and
  resume at ``position + 1`` with no tracking-gap window.
* **Per-event advance (at-least-once).** The checkpoint advances *after* the
  handler returns for an event. If the process dies mid-batch, the last events
  are re-delivered on restart — so a projection handler must be idempotent (pair
  it with the §12.1 inbox, or make the read-model write idempotent). This is the
  standard, honest projection contract.
* **Fail-stop.** If a handler raises, the subscription stops at the last good
  position, marks the checkpoint :attr:`CheckpointStatus.FAILED` with the error,
  and re-raises (or, in :meth:`run_until_caught_up`, returns the failure) — a bad
  event never silently advances past unprocessed work.
* **Backpressure / pacing** are the caller's: :meth:`run_once` does exactly one
  bounded page; :meth:`run_until_caught_up` drains to the live tail; the
  long-lived :meth:`run_forever` polls.

The subscription does not own a transaction. The *handler* is invoked with each
event and is expected to do its own unit-of-work (advance-checkpoint +
read-model write in one txn) when durability matters; for the simple in-memory
case the subscription advances an injected checkpoint store directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace

from app.eventsourcing.store.contracts import (
    Checkpoint,
    CheckpointStatus,
    CheckpointStore,
    EventStore,
    RecordedEvent,
)

#: A projection handler: called once per event, in global order.
EventHandler = Callable[[RecordedEvent], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SubscriptionResult:
    """Outcome of a subscription run pass."""

    processed: int
    position: int
    caught_up: bool
    failed: bool = False
    error: str | None = None

    @property
    def did_work(self) -> bool:
        return self.processed > 0


class CatchUpSubscription:
    """Drives a handler over the global log from a durable checkpoint."""

    def __init__(
        self,
        name: str,
        store: EventStore,
        checkpoints: CheckpointStore,
        handler: EventHandler,
        *,
        batch_size: int = 100,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be >= 1")
        self._name = name
        self._store = store
        self._checkpoints = checkpoints
        self._handler = handler
        self._batch_size = batch_size

    @property
    def name(self) -> str:
        return self._name

    async def position(self) -> int:
        """The current durable position (resume point - 1)."""
        return (await self._checkpoints.load(self._name)).position

    async def run_once(self) -> SubscriptionResult:
        """Process at most one batch from the current checkpoint.

        Returns a result describing how far it got. ``caught_up`` is True when the
        batch returned fewer events than ``batch_size`` (i.e. we reached the tail).
        On a handler error the checkpoint is marked FAILED and the error is
        captured in the result (not re-raised) so a poller can decide policy.
        """
        cp = await self._checkpoints.load(self._name)
        if cp.status is CheckpointStatus.PAUSED:
            return SubscriptionResult(processed=0, position=cp.position, caught_up=False)

        events = await self._store.read_all(from_position=cp.position, limit=self._batch_size)
        if not events:
            return SubscriptionResult(processed=0, position=cp.position, caught_up=True)

        processed = 0
        position = cp.position
        for event in events:
            try:
                await self._handler(event)
            except Exception as exc:
                # Stop at the last good position; persist the failure.
                failed_cp = replace(
                    cp,
                    position=position,
                    status=CheckpointStatus.FAILED,
                    events_processed=cp.events_processed + processed,
                    last_error=f"{type(exc).__name__}: {exc}"[:1000],
                )
                await self._checkpoints.save(failed_cp)
                return SubscriptionResult(
                    processed=processed,
                    position=position,
                    caught_up=False,
                    failed=True,
                    error=failed_cp.last_error,
                )
            position = event.global_position
            processed += 1

        advanced = replace(
            cp,
            position=position,
            status=CheckpointStatus.ACTIVE,
            events_processed=cp.events_processed + processed,
            last_error=None,
        )
        await self._checkpoints.save(advanced)
        caught_up = len(events) < self._batch_size
        return SubscriptionResult(processed=processed, position=position, caught_up=caught_up)

    async def run_until_caught_up(self, *, max_batches: int = 10_000) -> SubscriptionResult:
        """Drain forward until the live tail (or a failure / ``max_batches``)."""
        total = 0
        last = SubscriptionResult(processed=0, position=await self.position(), caught_up=True)
        for _ in range(max_batches):
            last = await self.run_once()
            total += last.processed
            if last.failed or last.caught_up:
                break
        return SubscriptionResult(
            processed=total,
            position=last.position,
            caught_up=last.caught_up,
            failed=last.failed,
            error=last.error,
        )

    async def resume(self) -> None:
        """Clear a PAUSED/FAILED state so the next run proceeds (ops control)."""
        cp = await self._checkpoints.load(self._name)
        await self._checkpoints.save(
            replace(cp, status=CheckpointStatus.ACTIVE, last_error=None)
        )

    async def pause(self) -> None:
        """Administratively pause the subscription (ops control)."""
        cp = await self._checkpoints.load(self._name)
        await self._checkpoints.save(replace(cp, status=CheckpointStatus.PAUSED))

    async def reset(self, *, to_position: int = 0) -> None:
        """Rewind the checkpoint (e.g. to rebuild a projection from scratch)."""
        if to_position < 0:
            raise ValueError("to_position must be >= 0")
        cp = await self._checkpoints.load(self._name)
        await self._checkpoints.save(
            Checkpoint(
                subscription=self._name,
                position=to_position,
                status=CheckpointStatus.ACTIVE,
                events_processed=cp.events_processed,
            )
        )

    async def run_forever(self, *, poll_interval_seconds: float = 0.5) -> None:  # pragma: no cover
        """Loop, draining to the tail then sleeping (long-lived worker)."""
        while True:
            result = await self.run_until_caught_up()
            if result.failed or not result.did_work:
                await asyncio.sleep(poll_interval_seconds)


__all__ = ["CatchUpSubscription", "EventHandler", "SubscriptionResult"]
