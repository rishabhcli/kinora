"""Deterministic time source for the saga engine (local copy; see DESIGN.md).

Every timeout, timer, and recovery-sweep "is this run stuck?" decision in the
engine reads *now* through a :class:`Clock`. Production uses :class:`SystemClock`;
tests use :class:`FakeClock`, which only advances when told to — so timeouts,
timers, and the stuck-workflow sweep are fully deterministic without
``asyncio.sleep`` or real wall-clock timers.

This mirrors :mod:`app.cache.clock` deliberately rather than importing it: per
the FINAL-ROUND isolation rule the ``app/sagas/`` package owns its own primitives
so it never couples to a sibling subsystem's lifecycle. The contract is identical
(``time`` for absolute deadlines, ``monotonic`` for elapsed durations) so a
production wiring can hand either one in.
"""

from __future__ import annotations

import threading
import time as _time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic-ish time source the engine reads *now* from."""

    def time(self) -> float:
        """Seconds since an arbitrary fixed epoch (used for absolute deadlines)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards."""
        ...


class SystemClock:
    """The real clock. ``time`` is wall time; ``monotonic`` is OS monotonic."""

    __slots__ = ()

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()


class FakeClock:
    """A controllable clock for deterministic tests.

    Both :meth:`time` and :meth:`monotonic` return ``start + elapsed`` where
    ``elapsed`` only grows via :meth:`advance` / :meth:`set`. Thread-safe so it
    can back concurrency tests without races.
    """

    __slots__ = ("_lock", "_now", "_start")

    def __init__(self, start: float = 1_700_000_000.0) -> None:
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
        """Move forward by ``seconds`` (must be >= 0); returns the new *now*."""
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        with self._lock:
            self._now += float(seconds)
            return self._now

    def set(self, now: float) -> None:
        """Pin absolute *now* (must not move backwards)."""
        with self._lock:
            if now < self._now:
                raise ValueError("FakeClock cannot move backwards")
            self._now = float(now)


#: A shared, stateless real clock; cheap to reuse.
SYSTEM_CLOCK: SystemClock = SystemClock()


__all__ = ["SYSTEM_CLOCK", "Clock", "FakeClock", "SystemClock"]
