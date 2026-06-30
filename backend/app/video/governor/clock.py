"""Time for the governor — an injectable monotonic clock + a deterministic fake.

Every time-sensitive decision in the governor (window rollover, Retry-After
expiry, throttle pacing, SLA windows, anti-starvation aging) reads the *current
second* through a :data:`Clock` callable. Production passes :func:`time.monotonic`;
tests pass a :class:`FakeClock` and advance it by hand so behaviour is exhaustively
reproducible with no sleeping and no wall-clock flake.

The governor deliberately reasons in **monotonic seconds** for everything that
must be immune to wall-clock jumps (pacing, breaker cooldowns), and keeps a
separate notion of *epoch* seconds only where calendar alignment matters (daily /
monthly quota windows). Both are sourced from the same injected clock so a fake
controls all of them at once.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

#: A monotonic clock source in seconds. Injectable everywhere in the governor so
#: tests advance virtual time instead of waiting on the wall clock.
Clock = Callable[[], float]

#: The default production clock.
monotonic: Clock = time.monotonic


@dataclass
class FakeClock:
    """A controllable clock for deterministic time-based tests.

    Usable directly as a :data:`Clock` (it is callable) and advanced explicitly::

        clock = FakeClock()
        ...                       # exercise some governor logic
        clock.advance(60.0)       # one minute later
        ...                       # the rpm window has now rolled over

    ``advance`` rejects negative deltas so a test can never silently move time
    backwards (which would corrupt window accounting).
    """

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> float:
        """Move the clock forward by ``seconds`` (>= 0); returns the new time."""
        if seconds < 0:
            raise ValueError("FakeClock cannot move backwards")
        self.now += seconds
        return self.now

    def set(self, when: float) -> None:
        """Jump the clock to an absolute ``when`` (must not be in the past)."""
        if when < self.now:
            raise ValueError("FakeClock cannot be set into the past")
        self.now = when


__all__ = ["Clock", "FakeClock", "monotonic"]
