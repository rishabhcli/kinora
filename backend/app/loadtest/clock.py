"""Time abstraction — virtual-time (deterministic tests) vs. wall-time (real runs).

A load harness has two irreconcilable needs:

* **Real runs** must sleep against the wall clock so requests actually arrive at
  the rate the operator asked for, and latencies are real network round-trips.
* **Tests** must be *deterministic and instant* — a 10-minute ramp, a Poisson
  arrival stream, and a coordinated-omission correction all have to produce the
  exact same numbers every run, in milliseconds of test time, with no flakiness
  and no real sleeping.

The whole harness therefore takes its time and its sleeping from an injected
:class:`Clock`. The two implementations are:

* :class:`WallClock` — ``now()`` is :func:`time.monotonic` and ``sleep`` awaits
  :func:`asyncio.sleep`. Used by the CLI / real runs.
* :class:`VirtualClock` — a discrete-event clock. ``now()`` returns the simulated
  time; ``sleep(dt)`` registers a wake-up at ``now + dt`` and yields control so
  *other* coroutines can run, but the simulated clock only advances when every
  runnable coroutine is parked on a ``sleep`` (i.e. the event loop is idle). A
  driver (:func:`VirtualClock.run`) repeatedly advances to the next scheduled
  wake-up. This lets thousands of virtual users / arrivals interleave correctly
  in *zero* real time, and the ordering is fully determined by the registered
  wake-up times (ties broken by insertion order), so tests are reproducible.

Both expose the *same* tiny async surface, so the generator never knows which it
is driving. Everything here is dependency-free and import-side-effect-free.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections.abc import Awaitable, Coroutine
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """The minimal time surface the harness depends on.

    Implementations must satisfy: ``now()`` is monotonically non-decreasing, and
    ``await sleep(dt)`` returns no earlier than ``dt`` seconds of *this clock's*
    time after the call (``dt <= 0`` returns promptly without advancing time).
    """

    def now(self) -> float:
        """Current time in seconds (monotonic; arbitrary epoch)."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Suspend the caller for ``seconds`` of this clock's time."""
        ...


class WallClock:
    """A real-time clock: monotonic ``now`` + ``asyncio.sleep``."""

    __slots__ = ()

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds > 0:
            await asyncio.sleep(seconds)


class VirtualClock:
    """A deterministic discrete-event clock for tests (no real sleeping).

    Time advances only when the driver (:meth:`run`) decides every coroutine is
    parked on a :meth:`sleep`. The driver pops the earliest scheduled wake-up,
    jumps the simulated clock to it, and releases that sleeper. Concurrency is
    real ``asyncio`` concurrency on the real loop — we never advance time inside
    a still-runnable task, which is what gives us coordinated-omission-correct,
    reproducible timings without wall-clock cost.
    """

    __slots__ = ("_now", "_heap", "_seq", "_activity")

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        # Min-heap of (wake_time, seq, future). ``seq`` breaks ties by insertion
        # order so the schedule is a total order (reproducible).
        self._heap: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()
        # Bumped on every observable scheduling event (a new sleep registered or
        # a sleeper released). The driver drains until this stops changing, which
        # is a robust quiescence signal independent of task internals.
        self._activity = 0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            # Yield once so a busy loop of zero-sleeps can't starve the driver,
            # but do not register a wake-up (time must not advance for dt<=0).
            await asyncio.sleep(0)
            return
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[None] = loop.create_future()
        wake_at = self._now + seconds
        heapq.heappush(self._heap, (wake_at, next(self._seq), fut))
        self._activity += 1
        await fut

    async def run(self, main: Coroutine[Any, Any, Any] | Awaitable[Any]) -> Any:
        """Drive ``main`` to completion, advancing virtual time as needed.

        Schedule ``main`` as a task, then loop:

        1. Drain the loop so every currently-runnable callback fires and any task
           that is going to ``sleep`` has registered its wake-up.
        2. If ``main`` finished, return its result.
        3. Otherwise advance to the next scheduled wake-up (releasing exactly the
           sleepers due at that instant), and repeat.

        The simulated clock therefore only moves when no task is runnable, which
        is the discrete-event invariant that makes the schedule reproducible.
        """
        task = asyncio.ensure_future(main)
        try:
            while True:
                await self._drain(task)
                if task.done():
                    break
                if not self._advance_due():
                    # ``main`` is not done but nothing is scheduled — it is
                    # awaiting something outside this clock (a wiring bug). Stop
                    # rather than spin so the stall surfaces as a hung task.
                    break
        finally:
            if not task.done():
                task.cancel()
        return await task

    def _advance_due(self) -> bool:
        """Advance to the next wake-up and release every sleeper due at it."""
        if not self._heap:
            return False
        next_time = self._heap[0][0]
        self._now = max(self._now, next_time)
        # Release every sleeper due at the new ``now`` so simultaneous arrivals
        # fire together and the clock advances by exactly one discrete step.
        while self._heap and self._heap[0][0] <= self._now:
            _wake_at, _seq, fut = heapq.heappop(self._heap)
            if not fut.done():
                self._activity += 1
                fut.set_result(None)
        return True

    async def _drain(self, task: asyncio.Future[Any]) -> None:
        """Yield until the loop is quiescent (no scheduling activity across a turn).

        Each ``await asyncio.sleep(0)`` lets one round of ready callbacks run. We
        loop until ``_activity`` is unchanged across two consecutive yields — i.e.
        no task registered a new sleep or was released in the interim, so every
        awoken coroutine has run to its next park point.

        The subtlety: an *unchanged* activity count is ambiguous at startup — it
        could mean "fully parked" or "nothing has started yet" (the driven task
        is still being scheduled onto the loop). So we only treat quiescence as
        final once there is something to react to: either ``task`` has finished
        or there is at least one sleeper registered. While neither holds we keep
        yielding (bounded) so the driven task gets scheduled and reaches its
        first ``sleep``. A generous iteration bound guards a pathological loop.
        """
        last = -1
        stable = 0
        for _ in range(1_000_000):
            await asyncio.sleep(0)
            current = self._activity
            unchanged = current == last
            last = current
            actionable = task.done() or bool(self._heap)
            if unchanged and actionable:
                stable += 1
                if stable >= 2:
                    return
            else:
                stable = 0
