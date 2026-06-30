"""Deterministic virtual clock for the autoscaler (no wall-clock dependency).

Every time-aware decision in this package — cooldown windows, hysteresis dwell,
latency-window aging, predictive look-ahead — reads "now" from an injected
:class:`Clock` instead of calling :func:`time.monotonic` directly. Production
wires :class:`MonotonicClock`; tests and the simulator wire :class:`VirtualClock`
and *advance time themselves*, so a controller run is bit-for-bit reproducible:
the same demand trace always yields the same :class:`~app.autoscale.controller.ScalingPlan`
sequence.

This is the load-bearing piece for the whole subsystem's testability. Cooldown
and hysteresis are inherently temporal; without a controllable clock their tests
would be timing-flaky (the exact failure mode flagged in the worker-test memory).
With it they are pure functions of (trace, advances).
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "MonotonicClock", "VirtualClock"]


@runtime_checkable
class Clock(Protocol):
    """A monotonic seconds source. ``now()`` is non-decreasing within a run."""

    def now(self) -> float:  # pragma: no cover - protocol
        """Return the current time in seconds (monotonic, arbitrary epoch)."""
        ...


class MonotonicClock:
    """Production clock backed by :func:`time.monotonic` (wall-clock, real time)."""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()


class VirtualClock:
    """A test/simulation clock the caller advances by hand.

    ``now()`` returns the accumulated time; :meth:`advance` moves it forward by a
    non-negative delta. Deliberately not threadsafe — the autoscaler control loop
    and the simulator are single-task by design.
    """

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, dt: float) -> float:
        """Advance the clock by ``dt`` seconds (must be >= 0). Returns the new now."""
        if dt < 0:
            raise ValueError("VirtualClock cannot move backwards")
        self._t += float(dt)
        return self._t

    def set(self, t: float) -> float:
        """Set absolute time; must not move backwards."""
        if t < self._t:
            raise ValueError("VirtualClock cannot move backwards")
        self._t = float(t)
        return self._t
