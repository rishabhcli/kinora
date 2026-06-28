"""Time source abstraction — the deterministic test harness foundation.

Every TTL / early-expiry decision in the cache layer reads "now" through a
:class:`Clock`. Production uses :class:`SystemClock` (monotonic wall clock);
tests use :class:`FakeClock`, which only advances when you tell it to. That
makes TTL expiry, probabilistic early refresh, and negative-cache windows fully
deterministic without ``time.sleep`` or real timers.

The clock exposes both a wall-style ``time()`` (seconds since an arbitrary
epoch, float) used for stored absolute expiry timestamps, and ``monotonic()``
used for elapsed-duration measurements. For :class:`FakeClock` the two advance
together so a single :meth:`FakeClock.advance` moves all cache deadlines.
"""

from __future__ import annotations

import threading
import time as _time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic-ish time source the cache reads ``now`` from."""

    def time(self) -> float:
        """Seconds since an arbitrary fixed epoch (used for absolute expiry)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards."""
        ...


class SystemClock:
    """The real clock. ``time`` is wall time; ``monotonic`` is the OS monotonic clock."""

    __slots__ = ()

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()


class FakeClock:
    """A controllable clock for deterministic tests.

    Both :meth:`time` and :meth:`monotonic` return ``start + elapsed`` where
    ``elapsed`` only grows via :meth:`advance` / :meth:`set`. Thread-safe so it
    can back concurrency tests (single-flight, stampede) without races.
    """

    __slots__ = ("_lock", "_now", "_start")

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._start = float(start)
        self._now = float(start)
        self._lock = threading.Lock()

    def time(self) -> float:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._now - self._start

    def advance(self, seconds: float) -> float:
        """Move the clock forward by ``seconds`` (must be >= 0); returns new now."""
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        with self._lock:
            self._now += float(seconds)
            return self._now

    def set(self, now: float) -> None:
        """Pin absolute now (must not move backwards)."""
        with self._lock:
            if now < self._now:
                raise ValueError("FakeClock cannot move backwards")
            self._now = float(now)


#: A module-level shared real clock; cheap to reuse (it is stateless).
SYSTEM_CLOCK: SystemClock = SystemClock()


__all__ = ["SYSTEM_CLOCK", "Clock", "FakeClock", "SystemClock"]
