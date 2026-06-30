"""Borrow/return leases + a fair FIFO waiter queue (pure async coordination).

A render borrows a warm session, uses it, and returns it. When all sessions are
leased and the pool is at ``max_size``, further borrows must *wait* — but fairly:
the first waiter to arrive is the first served (FIFO), never starved by a
late-comer. This module owns that coordination; the pool owns the sessions.

The lease itself is a small context-manager-friendly handle. The fairness queue
is a FIFO of one-shot :class:`asyncio.Future` s; a returning session hands itself
to the oldest waiter directly (a hand-off), so a freed session never races back
into the idle set ahead of someone already blocked on it. Borrow timeouts are
measured on the injected :class:`~app.video.warmpool.clock.Clock` so they are
deterministic under a virtual clock.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from .clock import Clock
from .protocols import ProviderId, ProviderSession


class LeaseError(Exception):
    """Base for lease problems."""


class LeaseTimeout(LeaseError):  # noqa: N818 - public name in subsystem contract
    """``borrow`` waited past its deadline without a session coming free."""

    def __init__(self, provider: ProviderId, waited_s: float) -> None:
        super().__init__(f"warm-pool borrow for {provider!r} timed out after {waited_s:.3f}s")
        self.provider = provider
        self.waited_s = waited_s


class PoolDraining(LeaseError):  # noqa: N818 - public name in subsystem contract
    """``borrow`` was rejected because the provider's pool is draining (unhealthy)."""

    def __init__(self, provider: ProviderId) -> None:
        super().__init__(f"warm-pool for {provider!r} is draining; not lending sessions")
        self.provider = provider


@dataclass(slots=True)
class Lease:
    """A borrowed session + bookkeeping; return it via the owning pool.

    Use it as an async context manager (``async with pool.borrow(...) as lease``)
    so the session is always returned, even on error. ``returned`` guards against
    a double-return.
    """

    provider: ProviderId
    session: ProviderSession
    leased_at: float
    lease_id: int
    returned: bool = False

    @property
    def handle(self) -> object:
        """The provider handle the caller actually renders through."""
        return self.session.handle


@dataclass(slots=True)
class _Waiter:
    """One parked borrower: a future a returning session is handed to."""

    future: asyncio.Future[ProviderSession]
    enqueued_at: float


@dataclass(slots=True)
class FairWaiterQueue:
    """FIFO hand-off queue for borrowers blocked on an exhausted pool (pure).

    A returning session calls :meth:`handoff`; if a waiter is parked it receives
    the session *directly* (FIFO order preserved) and the method reports the
    hand-off succeeded, so the pool knows not to re-shelve the session. Cancelled
    waiters are skipped transparently.
    """

    _clock: Clock
    _waiters: deque[_Waiter] = field(default_factory=deque)

    def __len__(self) -> int:
        return len(self._waiters)

    @property
    def waiting(self) -> int:
        """How many live borrowers are parked right now."""
        return sum(1 for w in self._waiters if not w.future.done())

    def park(self) -> asyncio.Future[ProviderSession]:
        """Register a new waiter at the back of the queue; returns its future."""
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[ProviderSession] = loop.create_future()
        self._waiters.append(_Waiter(future=fut, enqueued_at=self._clock.monotonic()))
        return fut

    def handoff(self, session: ProviderSession) -> bool:
        """Give ``session`` to the oldest live waiter (FIFO). ``True`` if handed off.

        Skips waiters whose future was already cancelled/resolved. Returns ``False``
        when no live waiter remains (the caller should re-shelve the session).
        """
        while self._waiters:
            waiter = self._waiters.popleft()
            if waiter.future.done():  # cancelled while waiting — skip it
                continue
            waiter.future.set_result(session)
            return True
        return False

    def fail_all(self, exc: Exception) -> None:
        """Reject every parked waiter with ``exc`` (used when the pool drains/closes)."""
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(exc)

    def drop_cancelled(self) -> None:
        """Prune already-resolved/cancelled waiters from the front (housekeeping)."""
        while self._waiters and self._waiters[0].future.done():
            self._waiters.popleft()


__all__ = [
    "FairWaiterQueue",
    "Lease",
    "LeaseError",
    "LeaseTimeout",
    "PoolDraining",
]
