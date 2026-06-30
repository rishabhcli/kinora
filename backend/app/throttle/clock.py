"""The time seam shared by the whole throttle fabric.

Every limiter, lease, and quota in :mod:`app.throttle` reads time through a
:class:`Clock` rather than calling ``time.monotonic`` directly. There are two
reasons this matters here specifically:

* **Determinism.** A rate limiter *is* a function of time — refill, window
  expiry, GCRA's theoretical-arrival-time all advance with the wall clock. The
  only way to test "after 0.5 s the bucket has half a token back" without
  ``sleep`` is to make time injectable. Tests wire a :class:`ManualClock` and
  step it by exact deltas.

* **Server-authoritative time.** In a distributed fabric the *limiter's* notion
  of "now" must be the **store's** clock, not each caller's wall clock — a
  caller with a skewed clock must not get a bigger or smaller window. The
  atomic compute-units (:mod:`app.throttle.units`) therefore take ``now`` as an
  explicit argument sourced from one clock per :class:`~app.throttle.client`,
  and the redis transport can supply ``TIME`` from the server (see
  :class:`app.throttle.transport.RedisScriptTransport`).

The clock unit is **seconds as a float** throughout. Milliseconds appear only at
the redis-emulator boundary (redis ``TIME`` is ``[secs, micros]``); the helpers
here convert at that one seam.
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic source of seconds-as-float. ``now()`` never goes backwards."""

    def now(self) -> float:
        """Return the current time in seconds (monotonic within one clock)."""
        ...


class MonotonicClock:
    """Production clock backed by :func:`time.monotonic`.

    Monotonic (not wall) so a system-clock adjustment can never make a window
    appear to run backwards. Process-local: only meaningful as *this process's*
    fallback when the store does not supply authoritative time.
    """

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()


class WallClock:
    """Wall-clock seconds (:func:`time.time`).

    Used where an *absolute* epoch is needed across processes that cannot share a
    server clock. Prefer the store's ``TIME`` when available; this is the
    cross-process fallback.
    """

    __slots__ = ()

    def now(self) -> float:
        return time.time()


class ManualClock:
    """A test clock you advance by hand. Time only moves when you move it.

    Thread-unsafe by design — throttle tests are single-threaded and assert exact
    timings. Use :meth:`advance` to step forward and :meth:`set` to jump.
    """

    __slots__ = ("_t",)

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> float:
        """Move time forward by ``seconds`` (must be >= 0); return the new now."""
        if seconds < 0:
            raise ValueError("ManualClock cannot move backwards")
        self._t += seconds
        return self._t

    def set(self, seconds: float) -> None:
        """Jump to an absolute time (must not move backwards)."""
        if seconds < self._t:
            raise ValueError("ManualClock cannot move backwards")
        self._t = float(seconds)


def seconds_to_redis_time(now: float) -> list[int]:
    """Render seconds-as-float as redis ``TIME`` form ``[secs, micros]``.

    The redis ``TIME`` command returns a two-element array of integer seconds and
    integer microseconds; the emulator mirrors that so unit scripts that read
    server time behave identically against the real driver.
    """
    secs = int(now)
    micros = int(round((now - secs) * 1_000_000))
    # Guard the rounding edge (e.g. 0.9999995 -> 1_000_000 micros).
    if micros >= 1_000_000:
        secs += 1
        micros -= 1_000_000
    return [secs, micros]


def redis_time_to_seconds(redis_time: list[int]) -> float:
    """Inverse of :func:`seconds_to_redis_time`."""
    secs, micros = redis_time
    return secs + micros / 1_000_000


__all__ = [
    "Clock",
    "ManualClock",
    "MonotonicClock",
    "WallClock",
    "redis_time_to_seconds",
    "seconds_to_redis_time",
]
