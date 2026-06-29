"""The transactional-outbox relay (§12.1 reliable publish).

The relay is the *publish* side of the outbox pattern: the store writes a
``pending`` outbox row atomically with each event; this relay periodically
claims due, pending rows, hands them to a :class:`~contracts.MessagePublisher`,
and marks them ``published``. A publish failure backs off with exponential delay
and, after ``max_attempts``, dead-letters the row (the §12.1 DLQ) so one
poison message never stalls the lane.

The relay is transport-agnostic and repository-agnostic: it drives any
:class:`~contracts.OutboxRepository` (the in-memory store, the Postgres repo) and
any :class:`~contracts.MessagePublisher`. :meth:`run_once` does a single drain
pass (the unit the conformance/integration suites assert on); :meth:`run_forever`
loops it with a poll interval for the worker process.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.eventsourcing.store.contracts import (
    MessagePublisher,
    OutboxRecord,
    OutboxRepository,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def backoff_delay(attempts: int, *, base_seconds: float, cap_seconds: float) -> timedelta:
    """Exponential backoff for the ``attempts``-th failure (0-based).

    attempts=0 → base, 1 → 2·base, 2 → 4·base, … capped at ``cap_seconds``.
    """
    delay = min(cap_seconds, base_seconds * (2 ** max(0, attempts)))
    return timedelta(seconds=delay)


@dataclass(frozen=True, slots=True)
class RelayResult:
    """Outcome of one :meth:`OutboxRelay.run_once` pass."""

    claimed: int
    published: int
    failed: int
    dead_lettered: int

    @property
    def did_work(self) -> bool:
        return self.claimed > 0


class OutboxRelay:
    """Claims pending outbox rows and publishes them with backoff + DLQ."""

    def __init__(
        self,
        repo: OutboxRepository,
        publisher: MessagePublisher,
        *,
        batch_size: int = 100,
        max_attempts: int = 8,
        base_backoff_seconds: float = 2.0,
        cap_backoff_seconds: float = 300.0,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("batch_size must be >= 1")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be >= 1")
        self._repo = repo
        self._publisher = publisher
        self._batch_size = batch_size
        self._max_attempts = max_attempts
        self._base = base_backoff_seconds
        self._cap = cap_backoff_seconds

    async def run_once(self, *, now: datetime | None = None) -> RelayResult:
        """Claim one batch, publish each row, mark published / failed.

        Each row is published independently so one failure doesn't block the
        rest of the batch. Successful rows are marked in a single
        :meth:`OutboxRepository.mark_published` call (cheap when backed by SQL).
        """
        now = now or _utcnow()
        claimed: list[OutboxRecord] = await self._repo.claim_batch(
            limit=self._batch_size, now=now
        )
        if not claimed:
            return RelayResult(0, 0, 0, 0)

        published_ids: list[str] = []
        failed = 0
        dead = 0
        for record in claimed:
            try:
                await self._publisher.publish(record)
            except Exception as exc:  # transient delivery failure
                attempts_after = record.attempts + 1
                is_dead = attempts_after >= self._max_attempts
                retry_at = now + backoff_delay(
                    record.attempts, base_seconds=self._base, cap_seconds=self._cap
                )
                await self._repo.mark_failed(
                    record.id,
                    error=f"{type(exc).__name__}: {exc}"[:1000],
                    retry_at=retry_at,
                    dead=is_dead,
                )
                failed += 1
                if is_dead:
                    dead += 1
            else:
                published_ids.append(record.id)

        if published_ids:
            await self._repo.mark_published(published_ids, now=now)

        return RelayResult(
            claimed=len(claimed),
            published=len(published_ids),
            failed=failed,
            dead_lettered=dead,
        )

    async def drain(self, *, max_passes: int = 1000) -> RelayResult:
        """Run passes until a pass claims nothing (or ``max_passes``).

        Returns the aggregate of every pass. Useful in tests to flush the whole
        backlog deterministically.
        """
        total = RelayResult(0, 0, 0, 0)
        for _ in range(max_passes):
            result = await self.run_once()
            total = RelayResult(
                claimed=total.claimed + result.claimed,
                published=total.published + result.published,
                failed=total.failed + result.failed,
                dead_lettered=total.dead_lettered + result.dead_lettered,
            )
            if not result.did_work:
                break
        return total

    async def run_forever(self, *, poll_interval_seconds: float = 1.0) -> None:  # pragma: no cover
        """Loop :meth:`run_once`, sleeping ``poll_interval`` between empty passes.

        Intended for a long-lived worker process; not exercised by the unit
        suite (it never terminates).
        """
        while True:
            result = await self.run_once()
            if not result.did_work:
                await asyncio.sleep(poll_interval_seconds)


__all__ = ["OutboxRelay", "RelayResult", "backoff_delay"]
