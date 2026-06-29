"""Time source abstraction — the deterministic test harness foundation.

Every wall-clock read in the CDC plane (polling cadence, lag estimation,
checkpoint timestamps) goes through a :class:`Clock`. Production uses
:class:`SystemClock`; tests use :class:`FakeClock`, which only advances when
told to. That makes polling intervals, watermark windows, and lag math fully
deterministic without ``time.sleep`` or real timers.

This mirrors ``app/cache/clock.py`` deliberately so the two deterministic test
harnesses behave identically; it is re-declared here (rather than imported) to
keep the CDC package self-contained and avoid a cross-package coupling that a
parallel facet might churn.
"""

from __future__ import annotations

import threading
import time as _time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic-ish time source the CDC plane reads ``now`` from."""

    def time(self) -> float:
        """Seconds since an arbitrary fixed epoch (wall-style, float)."""
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
    can back concurrency tests without races.
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
        """Move time forward by ``seconds`` (must be non-negative)."""
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        with self._lock:
            self._now += float(seconds)
            return self._now

    def set(self, wall: float) -> None:
        """Set the absolute wall time (must not move backwards)."""
        with self._lock:
            if wall < self._now:
                raise ValueError("FakeClock cannot move backwards")
            self._now = float(wall)


__all__ = ["Clock", "FakeClock", "SystemClock"]
