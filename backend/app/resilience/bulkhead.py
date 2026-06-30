"""A bulkhead — semaphore-based concurrency isolation per dependency.

Named after a ship's watertight compartments: cap the number of in-flight calls to
one dependency so a slow/stuck dependency can only consume *its own* slots and can't
exhaust the whole worker's task budget and drag every other dependency down with it.
A flapping image model shouldn't be able to starve chat or Redis.

Two limits:

* ``max_concurrency`` — slots that can run at once (the watertight compartment).
* ``max_queue`` — callers allowed to *wait* for a slot; beyond that, acquisition is
  shed immediately with :class:`~app.resilience.errors.BulkheadFull` (fail fast under
  overload instead of building an unbounded backlog). ``max_queue=0`` = no waiting.

An optional ``acquire_timeout_s`` bounds how long a queued caller waits before it,
too, is shed as :class:`BulkheadFull`. Waiting is implemented with an
:class:`asyncio.Semaphore` plus an explicit waiter counter (so the queue cap is
enforced) and the injected clock's ``sleep``-free timeout — we wrap the semaphore
acquire in :func:`asyncio.wait_for`, which uses the real loop timer; tests that need
determinism use ``max_queue=0`` (instant shed) so no wall-clock wait is involved.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import deque
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TypeVar

from app.core.logging import get_logger

from .errors import BulkheadFull

logger = get_logger("app.resilience.bulkhead")

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BulkheadConfig:
    """Tunables for a :class:`Bulkhead`."""

    max_concurrency: int = 8
    #: How many callers may *wait* for a slot. 0 = shed immediately when full.
    max_queue: int = 64
    #: Optional ceiling (seconds) on how long a queued caller waits. ``None`` = no
    #: cap (wait until a slot frees or the queue limit sheds you).
    acquire_timeout_s: float | None = None

    def __post_init__(self) -> None:
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if self.max_queue < 0:
            raise ValueError("max_queue must be >= 0")
        if self.acquire_timeout_s is not None and self.acquire_timeout_s <= 0:
            raise ValueError("acquire_timeout_s must be > 0 when set")


@dataclass(frozen=True, slots=True)
class BulkheadSnapshot:
    """An immutable view of bulkhead utilization (telemetry + tests)."""

    name: str
    max_concurrency: int
    active: int
    waiting: int
    max_queue: int
    total_admitted: int
    total_rejected: int


class Bulkhead:
    """A per-dependency concurrency limiter. Use via :meth:`slot` or :meth:`run`."""

    def __init__(self, name: str, config: BulkheadConfig | None = None) -> None:
        self.name = name
        self.config = config or BulkheadConfig()
        self._active = 0
        self._waiting = 0
        self._total_admitted = 0
        self._total_rejected = 0
        # FIFO queue of waiters parked on a free slot. We manage slots with our own
        # counter + futures rather than asyncio.Semaphore so the non-blocking
        # fast-path admit is exact (no peeking at a private semaphore counter) and
        # the queue cap / acquire-timeout accounting stays consistent.
        self._waiters: deque[asyncio.Future[None]] = deque()

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return self._waiting

    @property
    def available(self) -> int:
        return self.config.max_concurrency - self._active

    def snapshot(self) -> BulkheadSnapshot:
        return BulkheadSnapshot(
            name=self.name,
            max_concurrency=self.config.max_concurrency,
            active=self._active,
            waiting=self._waiting,
            max_queue=self.config.max_queue,
            total_admitted=self._total_admitted,
            total_rejected=self._total_rejected,
        )

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold a concurrency slot for the duration of the ``async with`` block.

        Raises :class:`BulkheadFull` immediately if the slot is unavailable and the
        wait queue is at capacity, or after ``acquire_timeout_s`` if a queued wait
        times out.
        """
        await self._acquire()
        try:
            yield
        finally:
            self._release()

    async def run(self, coro: Awaitable[T]) -> T:
        """Await ``coro`` while holding a slot. Convenience over :meth:`slot`."""
        async with self.slot():
            return await coro

    async def _acquire(self) -> None:
        # Fast path: a slot is free right now (no waiting, no suspension).
        if self._active < self.config.max_concurrency:
            self._active += 1
            self._total_admitted += 1
            return
        # No free slot. Are we allowed to wait?
        if self._waiting >= self.config.max_queue:
            self._total_rejected += 1
            logger.warning(
                "resilience.bulkhead.shed",
                bulkhead=self.name,
                active=self._active,
                waiting=self._waiting,
            )
            raise BulkheadFull(
                f"bulkhead {self.name!r} full "
                f"(active={self._active}, queue={self._waiting}/{self.config.max_queue})",
                name=self.name,
            )
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        self._waiting += 1
        try:
            if self.config.acquire_timeout_s is not None:
                await asyncio.wait_for(waiter, timeout=self.config.acquire_timeout_s)
            else:
                await waiter
        except TimeoutError as exc:
            self._total_rejected += 1
            # Race: a _release() may have granted us the slot (set_result) in the
            # same tick the timeout fired. If so the slot was already transferred to
            # us, so we must hand it back rather than just dropping the waiter, or
            # the slot leaks. Otherwise just drop the pending waiter.
            if waiter.done() and not waiter.cancelled():
                self._release()
            else:
                self._discard_waiter(waiter)
            raise BulkheadFull(
                f"bulkhead {self.name!r} acquire timed out after "
                f"{self.config.acquire_timeout_s}s",
                name=self.name,
            ) from exc
        finally:
            self._waiting -= 1
        # Woken by _release(), which already accounted the slot to us.
        self._total_admitted += 1

    def _discard_waiter(self, waiter: asyncio.Future[None]) -> None:
        with contextlib.suppress(ValueError):
            self._waiters.remove(waiter)  # may already be popped by a racing release

    def _release(self) -> None:
        # Hand the freed slot directly to the next waiter (keeping _active steady),
        # or drop the active count if nobody is queued.
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)  # the slot stays "active", now owned by them
                return
        self._active = max(0, self._active - 1)


__all__ = [
    "Bulkhead",
    "BulkheadConfig",
    "BulkheadSnapshot",
]
