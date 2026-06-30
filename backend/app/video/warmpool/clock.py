"""Time + async-sleep abstraction — the deterministic test foundation.

Every deadline in the warm pool (idle eviction, health-recycle interval, lease
timeout, keep-alive tick, latency-sample staleness) is read through a
:class:`Clock`. Production uses :class:`SystemClock`; tests use
:class:`VirtualClock`, which only advances when told to, so every timer is
exercised without ``asyncio.sleep`` or real wall-time.

This is a **local** protocol on purpose. The FINAL-round warm pool may not import
the clock from another round's package, and the cache-layer ``app.cache.clock``
is intentionally *not* a dependency (the warm pool is awaitable: it needs an
injectable async ``sleep`` to coordinate fairness waiters and the keep-alive
loop, which the cache clock has no concept of). The shape deliberately mirrors
``app.cache.clock`` (``time`` + ``monotonic``) so it reads as familiar, and adds
the async ``sleep`` the pool's coroutines block on.

The virtual clock's :meth:`VirtualClock.sleep` is a *cooperative* timer: a sleeper
registers a wake deadline and parks on an :class:`asyncio.Event`; the test driver
calls :meth:`VirtualClock.advance`, which fires every deadline that has elapsed,
in order. That lets a single ``await clock.advance(idle_ttl_s)`` deterministically
release every coroutine that was waiting on a shorter delay — the same trick the
scheduler simulation harness uses, but local to this package.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import threading
import time as _time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic time source the warm pool reads ``now`` from, plus async sleep."""

    def time(self) -> float:
        """Seconds since an arbitrary fixed epoch (absolute deadlines / telemetry)."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary start; never goes backwards (elapsed durations)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the calling coroutine for ``seconds`` (may be cancelled)."""
        ...


class SystemClock:
    """The real clock: ``monotonic`` durations and ``asyncio.sleep`` delays."""

    __slots__ = ()

    def time(self) -> float:
        return _time.time()

    def monotonic(self) -> float:
        return _time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class VirtualClock:
    """A controllable, awaitable clock for deterministic concurrency tests.

    ``time`` and ``monotonic`` return ``start + elapsed``; elapsed only grows via
    :meth:`advance` / :meth:`set`. ``sleep`` parks the caller on a per-sleeper
    event keyed by an absolute wake deadline; :meth:`advance` walks the deadline
    heap and fires every timer that has come due, **in deadline order**, so
    wake-ups are fully ordered and reproducible. A zero-second sleep yields once
    (so a busy keep-alive loop still cedes control) but never blocks.
    """

    __slots__ = ("_counter", "_heap", "_lock", "_now", "_start")

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._start = float(start)
        self._now = float(start)
        self._lock = threading.Lock()
        # Min-heap of (deadline, seq, event); seq breaks ties deterministically.
        self._heap: list[tuple[float, int, asyncio.Event]] = []
        self._counter = itertools.count()

    # --- read side ------------------------------------------------------- #

    def time(self) -> float:
        with self._lock:
            return self._now

    def monotonic(self) -> float:
        with self._lock:
            return self._now - self._start

    @property
    def pending_timers(self) -> int:
        """How many sleepers are currently parked (for leak assertions)."""
        with self._lock:
            return len(self._heap)

    # --- sleep / advance ------------------------------------------------- #

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            # Cooperative yield without blocking the virtual clock.
            await asyncio.sleep(0)
            return
        event = asyncio.Event()
        with self._lock:
            deadline = self._now + float(seconds)
            heapq.heappush(self._heap, (deadline, next(self._counter), event))
        await event.wait()

    async def advance(self, seconds: float) -> float:
        """Move time forward by ``seconds`` (>= 0), firing every due timer in order.

        Returns the new ``now``. After each batch of wake-ups we yield to the event
        loop so released coroutines run (and possibly register *new* timers) before
        the next batch — making cascades (a woken sleeper sleeps again) deterministic.
        """
        if seconds < 0:
            raise ValueError("VirtualClock cannot move backwards")
        with self._lock:
            target = self._now + float(seconds)
        await self._run_to(target)
        return target

    async def set(self, now: float) -> float:
        """Pin absolute ``now`` (must not move backwards), firing due timers."""
        with self._lock:
            if now < self._now:
                raise ValueError("VirtualClock cannot move backwards")
        await self._run_to(float(now))
        return now

    @staticmethod
    async def _settle() -> None:
        """Yield until the event loop is quiescent (bounded).

        Freshly-spawned coroutines often need several scheduling hops before they
        register their timer (``task → await fut → spawn timer → timer awaits
        clock.sleep``). A single ``await asyncio.sleep(0)`` only advances one hop, so
        ``advance`` would race ahead of a not-yet-parked sleeper. Yielding a small,
        bounded number of times lets such chains settle deterministically without an
        unbounded busy-loop. The bound is generous (the longest chain in the pool is
        a handful of hops) and harmless when the loop is already idle.
        """
        for _ in range(16):
            await asyncio.sleep(0)

    async def _run_to(self, target: float) -> None:
        # Let any just-spawned coroutines register their timers before we look.
        await self._settle()
        while True:
            with self._lock:
                if not self._heap or self._heap[0][0] > target:
                    self._now = target
                    fired: list[asyncio.Event] = []
                else:
                    deadline = self._heap[0][0]
                    self._now = deadline
                    fired = []
                    while self._heap and self._heap[0][0] <= deadline:
                        _, _, event = heapq.heappop(self._heap)
                        fired.append(event)
            for event in fired:
                event.set()
            # Let the released coroutines run (and register new timers) before
            # deciding what fires next — makes cascades deterministic.
            await self._settle()
            with self._lock:
                done = not self._heap or self._heap[0][0] > target
            if done:
                with self._lock:
                    self._now = target
                return


#: A module-level shared real clock; stateless, cheap to reuse.
SYSTEM_CLOCK: SystemClock = SystemClock()


__all__ = ["SYSTEM_CLOCK", "Clock", "SystemClock", "VirtualClock"]
