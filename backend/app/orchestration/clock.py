"""Injectable clocks for the orchestration layer (deterministic tests, real prod).

Every time-dependent decision in :mod:`app.orchestration` — lease expiry, dead-
worker detection, heartbeat staleness — reads "now" from a :class:`Clock` rather
than calling ``time``. Production wires :class:`MonotonicClock`; tests wire
:class:`VirtualClock` and advance it by hand, so lease-acquire/expire/reassign,
work-stealing, and dead-worker recovery are fully deterministic with no sleeps.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "MonotonicClock", "VirtualClock"]


@runtime_checkable
class Clock(Protocol):
    """A monotonic millisecond clock."""

    def now_ms(self) -> int:
        """Current time in monotonic milliseconds."""
        ...


class MonotonicClock:
    """The production clock: ``time.monotonic`` rendered as integer milliseconds."""

    __slots__ = ()

    def now_ms(self) -> int:
        return int(time.monotonic() * 1000.0)


class VirtualClock:
    """A hand-advanced clock for deterministic tests.

    Starts at ``start_ms`` (default 0) and only moves when :meth:`advance` /
    :meth:`set` are called, so a test can place a lease, jump past its TTL, and
    assert it expired — with no real waiting and no flakiness.
    """

    __slots__ = ("_now",)

    def __init__(self, start_ms: int = 0) -> None:
        self._now = int(start_ms)

    def now_ms(self) -> int:
        return self._now

    def advance(self, delta_ms: int) -> int:
        """Move the clock forward by ``delta_ms`` (must be non-negative)."""
        if delta_ms < 0:
            raise ValueError("cannot rewind a VirtualClock")
        self._now += int(delta_ms)
        return self._now

    def set(self, now_ms: int) -> int:
        """Jump to an absolute time (must not be earlier than now)."""
        if now_ms < self._now:
            raise ValueError("cannot rewind a VirtualClock")
        self._now = int(now_ms)
        return self._now
