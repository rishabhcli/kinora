"""Time + sleep abstraction — the determinism foundation for every policy here.

Every wait, deadline, cooldown and backoff in this package reads "now" and
"sleeps" through these seams instead of touching :mod:`time` / :func:`asyncio.sleep`
directly. Production wires :data:`SYSTEM_CLOCK` + :func:`asyncio.sleep`; tests wire
:class:`ManualClock` whose :meth:`ManualClock.sleep` *advances the clock instead of
waiting*. That is what lets a retry loop with multi-second exponential backoff run
to completion in microseconds, deterministically, with no real timers.

The shape deliberately mirrors :mod:`app.cache.clock` (``time`` / ``monotonic``)
and the provider-resilience ``FakeClock`` so the two layers feel the same, but this
one bundles the *async sleep* into the clock object — resilience policies need a
sleeper far more than the cache layer does, and keeping the pair together means a
single injected object controls both elapsed time and waiting.
"""

from __future__ import annotations

import asyncio
import threading
import time as _time
from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

#: A bare monotonic time source (seconds, float, never decreasing). Accepted
#: anywhere a full :class:`Clock` would be overkill — e.g. a breaker that only
#: needs ``monotonic()``.
MonotonicFn = Callable[[], float]
#: An async sleep: ``await sleep(seconds)``. Tests pass one that advances a clock.
AsyncSleep = Callable[[float], Awaitable[None]]


@runtime_checkable
class Clock(Protocol):
    """A time source + async sleeper the resilience policies read ``now`` from.

    Implementations must keep :meth:`monotonic` non-decreasing; :meth:`time` is a
    wall-style epoch used only for human-facing telemetry / logs (deadlines and
    cooldowns are always computed off :meth:`monotonic`).
    """

    def time(self) -> float:
        """Seconds since an arbitrary fixed epoch (wall-ish; telemetry only)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Await ``seconds`` (clamped at 0). The single waiting seam."""
        ...


class SystemClock:
    """The real clock. ``monotonic`` is the OS monotonic clock; ``sleep`` is real."""

    __slots__ = ()

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))


class ManualClock:
    """A controllable clock for deterministic, instant tests.

    :meth:`monotonic` / :meth:`time` return ``start + elapsed`` where ``elapsed``
    grows only via :meth:`advance` or via :meth:`sleep` (which advances by the
    requested seconds *instead of* waiting). :meth:`sleep` still yields to the event
    loop once (``asyncio.sleep(0)``) so concurrent tasks make progress and a tight
    retry/acquire loop cannot starve siblings — virtual time, real cooperative
    scheduling. Thread-safe so it can back concurrency tests.
    """

    __slots__ = ("_lock", "_now", "_start", "slept")

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._start = float(start)
        self._now = float(start)
        self._lock = threading.Lock()
        #: Every ``sleep`` duration, in call order — lets a test assert the exact
        #: backoff ladder a retry loop walked without inspecting internals.
        self.slept: list[float] = []

    def time(self) -> float:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._now - self._start

    def advance(self, seconds: float) -> float:
        """Move the clock forward by ``seconds`` (>= 0); returns the new ``now``."""
        if seconds < 0:
            raise ValueError("ManualClock cannot move backwards")
        with self._lock:
            self._now += float(seconds)
            return self._now

    async def sleep(self, seconds: float) -> None:
        """Advance virtual time by ``seconds`` instead of waiting; records it."""
        secs = max(float(seconds), 0.0)
        with self._lock:
            self.slept.append(secs)
            self._now += secs
        await asyncio.sleep(0)


#: A module-level shared real clock; stateless, cheap to reuse.
SYSTEM_CLOCK: SystemClock = SystemClock()


__all__ = [
    "SYSTEM_CLOCK",
    "AsyncSleep",
    "Clock",
    "ManualClock",
    "MonotonicFn",
    "SystemClock",
]
