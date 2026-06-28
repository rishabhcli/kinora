"""Time sources for the jobs framework.

Every time-dependent decision in this package — when a cron/interval trigger
next fires, whether a backoff delay has elapsed, when a lease expires — is taken
against an injected :class:`Clock` rather than the wall clock. Production wires
:class:`SystemClock`; tests wire :class:`ManualClock` so the scheduler/worker
loops run in *virtual* time and assertions are exact (no ``sleep``, no flakiness).

The clock exposes both a monotonic-ish ``now()`` (a timezone-aware UTC datetime
used for scheduling math) and an ``async sleep()`` so the loops can ``await`` a
delay that, under the manual clock, is satisfied by advancing virtual time.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A time source: a UTC ``now`` plus an awaitable ``sleep``."""

    def now(self) -> datetime:
        """The current instant as a timezone-aware UTC :class:`datetime`."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend for ``seconds`` (real time, or virtual under a manual clock)."""
        ...


class SystemClock:
    """The production clock: wall-clock UTC + a real :func:`asyncio.sleep`."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class ManualClock:
    """A deterministic, advanceable virtual clock for tests.

    ``now()`` returns the current virtual instant; ``advance()`` moves it forward
    and wakes any sleeper whose deadline has passed. ``sleep()`` registers a
    deadline and yields control until virtual time reaches it — so a test can
    drive a scheduler/worker loop one tick at a time with full determinism.

    The clock is single-event-loop; sleepers are stored as ``(deadline, Event)``
    pairs and resolved on :meth:`advance`.
    """

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)
        if self._now.tzinfo is None:
            self._now = self._now.replace(tzinfo=UTC)
        self._sleepers: list[tuple[datetime, asyncio.Event]] = []

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            # Yield once so cooperative loops still interleave on a zero sleep.
            await asyncio.sleep(0)
            return
        deadline = self._now + timedelta(seconds=seconds)
        event = asyncio.Event()
        self._sleepers.append((deadline, event))
        await event.wait()

    async def advance(self, seconds: float) -> None:
        """Move virtual time forward by ``seconds``, releasing due sleepers.

        Releases sleepers whose deadline is at or before the new instant, then
        yields control so the woken coroutines make progress before the caller
        continues. Safe to call with any non-negative delta.
        """
        if seconds < 0:
            raise ValueError("cannot advance time backwards")
        self._now = self._now + timedelta(seconds=seconds)
        self._wake_due()
        # Let woken coroutines run before returning to the caller.
        await asyncio.sleep(0)

    async def advance_to(self, instant: datetime) -> None:
        """Advance virtual time to an absolute UTC ``instant`` (no-op if past)."""
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=UTC)
        if instant <= self._now:
            self._wake_due()
            await asyncio.sleep(0)
            return
        await self.advance((instant - self._now).total_seconds())

    def _wake_due(self) -> None:
        still_waiting: list[tuple[datetime, asyncio.Event]] = []
        for deadline, event in self._sleepers:
            if deadline <= self._now:
                event.set()
            else:
                still_waiting.append((deadline, event))
        self._sleepers = still_waiting

    @property
    def pending_sleepers(self) -> int:
        """How many coroutines are currently parked in :meth:`sleep`."""
        return len(self._sleepers)


__all__ = ["Clock", "ManualClock", "SystemClock"]
