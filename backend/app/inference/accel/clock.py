"""Injected clock — the deterministic time seam for the whole accel layer.

Every latency measurement, hedging delay, staleness/TTL decision, and rate
budget in :mod:`app.inference.accel` reads time through a :class:`Clock` rather
than calling :func:`time.monotonic` directly. Production wires in
:class:`SystemClock`; tests wire in :class:`FakeClock` so nothing sleeps and the
accept/expire/race math is reproducible to the microsecond.

This mirrors the convention already used by ``app.cache.clock`` (a separate,
general cache); the accel layer keeps its own copy so the two subsystems stay
independently ownable.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic + wall clock the accel layer can read deterministically."""

    def monotonic(self) -> float:
        """Monotonic seconds for measuring elapsed durations (never decreases)."""
        ...

    def time(self) -> float:
        """Wall-clock epoch seconds, for TTL / staleness comparisons."""
        ...


class SystemClock:
    """Real wall + monotonic clock (production default)."""

    __slots__ = ()

    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()


class FakeClock:
    """Deterministic test clock: time only moves when you :meth:`advance` it.

    Both the wall and monotonic readings advance in lock-step, so a test can
    assert on a latency *and* a staleness decision from the same advance.
    """

    __slots__ = ("_mono", "_wall")

    def __init__(self, *, start: float = 1_700_000_000.0) -> None:
        self._wall = float(start)
        self._mono = 0.0

    def monotonic(self) -> float:
        return self._mono

    def time(self) -> float:
        return self._wall

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by ``seconds`` (must be non-negative)."""
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self._mono += seconds
        self._wall += seconds

    def set_wall(self, epoch_seconds: float) -> None:
        """Jump the *wall* clock (e.g. to simulate a versioned-asset rewrite).

        The monotonic clock is unaffected — wall jumps must not corrupt elapsed
        measurements that straddle them.
        """
        if epoch_seconds < self._wall:
            raise ValueError("FakeClock wall time cannot move backwards")
        self._wall = float(epoch_seconds)


#: Process-wide singleton for callers that do not inject their own.
SYSTEM_CLOCK: Clock = SystemClock()


__all__ = ["SYSTEM_CLOCK", "Clock", "FakeClock", "SystemClock"]
