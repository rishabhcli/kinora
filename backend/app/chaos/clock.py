"""The chaos framework's time source — the deterministic-test foundation.

Every duration the orchestrated chaos layer cares about (how long a fault has
been armed, when the next scheduled fault fires, how long a steady-state probe
has been breaching before auto-abort) is measured against a :class:`Clock`.
Production uses :class:`SystemClock`; tests drive :class:`VirtualClock`, which
only advances when told to, so a whole game-day — fault schedule, steady-state
polling, abort window — replays deterministically with **zero real sleeping**.

This is a *local* protocol (not imported from any sibling resilience package):
the orchestrated chaos layer owns its own clock so it can be vendored / tested
in isolation. It mirrors the cache-layer clock's shape (``time`` + ``monotonic``)
so the two read interchangeably, but the chaos clock additionally exposes an
async :meth:`Clock.sleep` seam: a real game-day *waits* between schedule steps,
and the virtual clock turns that wait into an instant clock advance.
"""

from __future__ import annotations

import asyncio
import threading
import time as _time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic time source the chaos runner reads ``now`` from and sleeps on."""

    def time(self) -> float:
        """Seconds since an arbitrary fixed epoch (wall-style; for report stamps)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards (for elapsed)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Asynchronously wait ``seconds`` (real clock yields; virtual advances)."""
        ...


class SystemClock:
    """The real clock: wall ``time``, OS ``monotonic``, real ``asyncio.sleep``."""

    __slots__ = ()

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class VirtualClock:
    """A controllable, thread-safe clock for deterministic game-day tests.

    ``time`` and ``monotonic`` both return ``start + elapsed`` where ``elapsed``
    only grows. :meth:`sleep` does **not** block: it advances the clock by the
    requested amount and returns after yielding control once (so concurrent
    tasks awaiting the same clock interleave fairly) — never a real timer.
    """

    __slots__ = ("_lock", "_now", "_start", "slept_for")

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._start = float(start)
        self._now = float(start)
        self._lock = threading.Lock()
        #: Cumulative virtual seconds slept — handy for test assertions.
        self.slept_for: float = 0.0

    def time(self) -> float:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._now - self._start

    def advance(self, seconds: float) -> float:
        """Move the clock forward by ``seconds`` (must be >= 0); returns new now."""
        if seconds < 0:
            raise ValueError("VirtualClock cannot move backwards")
        with self._lock:
            self._now += float(seconds)
            return self._now

    async def sleep(self, seconds: float) -> None:
        """Advance the virtual clock by ``seconds`` and yield once (no real wait)."""
        secs = max(0.0, float(seconds))
        with self._lock:
            self._now += secs
            self.slept_for += secs
        # Yield control so sibling tasks observe the advanced clock — but never
        # touch a real timer, keeping the whole game-day instant + deterministic.
        await asyncio.sleep(0)


#: A module-level shared real clock; cheap to reuse (it is stateless).
SYSTEM_CLOCK: SystemClock = SystemClock()


__all__ = ["SYSTEM_CLOCK", "Clock", "SystemClock", "VirtualClock"]
