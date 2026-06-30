"""A virtual monotonic clock for the QoS fabric (deterministic tests, real prod).

Every QoS policy reads time through a :class:`Clock` so the same code is driven
by wall-clock seconds in production and by a hand-advanced :class:`VirtualClock`
in tests. Time is always in **float seconds** (monotonic), so deadline math,
aging windows, and load-shed decisions are reproducible bit-for-bit under a
synthetic load harness with no infra, no network, and no ``asyncio.sleep``.
"""

from __future__ import annotations

import time
from typing import Protocol


class Clock(Protocol):
    """The slice of time every QoS policy reads — monotonic float seconds."""

    def now(self) -> float:
        """The current monotonic time, in seconds."""
        ...


class WallClock:
    """The production clock: :func:`time.monotonic`."""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()


class VirtualClock:
    """A hand-advanced monotonic clock for deterministic tests.

    Starts at ``start`` and only moves when :meth:`advance` or :meth:`set` is
    called, so a whole load scenario plays out in virtual time with no sleeping.
    Time never goes backwards (a monotonic clock invariant) — :meth:`set` to an
    earlier value raises.
    """

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        """Move time forward by ``seconds`` (must be ``>= 0``); return the new time."""
        if seconds < 0:
            raise ValueError("virtual clock cannot move backwards")
        self._t += float(seconds)
        return self._t

    def set(self, t: float) -> float:
        """Jump to absolute time ``t`` (must not be before the current time)."""
        if t < self._t:
            raise ValueError("virtual clock cannot move backwards")
        self._t = float(t)
        return self._t


__all__ = ["Clock", "VirtualClock", "WallClock"]
