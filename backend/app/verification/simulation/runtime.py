"""The :class:`Simulation` runtime: the seam that lets one synchronous, virtual-time
event loop drive the real ``async`` control-plane services deterministically.

The tension this module resolves: Kinora's :class:`~app.scheduler.service.SchedulerService`
and :class:`~app.queue.redis_queue.RedisRenderQueue` are ``async`` (they were
written for production, where redis IO really suspends). But a FoundationDB-style
simulator must own *both* scheduling and time, and Python's :mod:`asyncio` cedes
neither — its task wakeups are OS-ordered and its clock is wall-clock.

The resolution rests on one fact established in :mod:`redis_sim`: inside the
simulation, none of the awaited operations ever *truly* suspend. ``FakeAsyncRedis``
completes synchronously, the injected budget/shot/event collaborators complete
synchronously, and there is no ``asyncio.sleep`` on the hot path. So every
coroutine the simulation invokes runs straight to completion on a single
``run_until_complete``. That lets the runtime treat an ``async`` call as an atomic
step on the *virtual* timeline: the synchronous :class:`~app.verification.simulation.core.EventLoop`
decides *when* (virtual time), and :meth:`Simulation.run_sync` executes the
coroutine *to completion at that instant*. Time only moves when the event loop
pops the next event — never inside a coroutine.

So the architecture is:

* :class:`~app.verification.simulation.core.EventLoop` + :class:`SimClock` — the
  single virtual timeline and the only clock.
* A private :mod:`asyncio` loop, used purely as a coroutine *runner* (never as a
  scheduler — it never sleeps, never orders concurrent tasks). :meth:`run_sync`
  drives one coroutine to completion at the current virtual instant.
* :class:`Buggify` + the seams (:mod:`network`, :mod:`storage`, :mod:`redis_sim`)
  — the deterministic adversary, all reading the virtual clock.

:class:`Simulation` owns these and exposes the handful of primitives the
:mod:`~app.verification.simulation.system` wiring needs: schedule a virtual event,
run a coroutine now, advance/drain time, and read the shared PRNG / Buggify.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from app.verification.simulation.buggify import Buggify, BuggifyLog
from app.verification.simulation.core import EventHandle, EventLoop, Prng, SimClock
from app.verification.simulation.faults import FaultSchedule

_T = TypeVar("_T")


class Simulation:
    """A single deterministic simulation run, parameterised by a fault schedule.

    Construct one per ``(seed, profile)`` — the run is then a pure function of that
    schedule. The runtime hands out *named* PRNG splits so each subsystem (faults,
    reader workload, worker timing, id generation) draws from an independent,
    seed-stable stream.
    """

    __slots__ = (
        "schedule",
        "clock",
        "loop",
        "prng",
        "buggify",
        "_buggify_log",
        "_aio",
        "_owns_aio",
        "_subsystem_prngs",
    )

    def __init__(
        self,
        schedule: FaultSchedule,
        *,
        start_ms: int = 0,
        aio_loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.schedule = schedule
        self.clock = SimClock(start_ms)
        self.loop = EventLoop(self.clock)

        # Root PRNG → independent, labelled subsystem streams. Faults take the
        # first split so adding a *workload* draw never shifts the fault pattern.
        self.prng = Prng(schedule.seed)
        self._subsystem_prngs: dict[str, Prng] = {}
        fault_prng = self._stream("faults")

        self._buggify_log = BuggifyLog()
        self.buggify = Buggify(
            schedule.profile,
            fault_prng,
            self.clock.as_callable_ms(),
            log=self._buggify_log,
        )

        # A private asyncio loop used only as a coroutine *runner*. We create and
        # own one unless the caller supplies theirs (e.g. nested under pytest).
        if aio_loop is not None:
            self._aio = aio_loop
            self._owns_aio = False
        else:
            self._aio = asyncio.new_event_loop()
            self._owns_aio = True

    # ----------------------------------------------------------------------- #
    # PRNG streams
    # ----------------------------------------------------------------------- #

    def _stream(self, label: str) -> Prng:
        """Internal: the labelled stream, created once per label."""
        prng = self._subsystem_prngs.get(label)
        if prng is None:
            prng = self.prng.split(label)
            self._subsystem_prngs[label] = prng
        return prng

    def stream(self, label: str) -> Prng:
        """An independent, seed-stable PRNG for a named subsystem.

        Calling ``stream("worker")`` always returns the same stream within a run;
        different labels are independent. This is the mechanism that keeps a
        regression seed stable as subsystems gain or lose random draws.
        """
        return self._stream(label)

    # ----------------------------------------------------------------------- #
    # Virtual time + coroutine running
    # ----------------------------------------------------------------------- #

    @property
    def now_ms(self) -> int:
        """Current virtual time (ms)."""
        return self.clock.now_ms

    def at(self, t_ms: int, fn: Callable[[int], None], *, label: str = "") -> EventHandle:
        """Schedule ``fn`` at absolute virtual time ``t_ms``."""
        return self.loop.call_at(t_ms, fn, label=label)

    def after(self, delay_ms: int, fn: Callable[[int], None], *, label: str = "") -> EventHandle:
        """Schedule ``fn`` ``delay_ms`` from now."""
        return self.loop.call_after(delay_ms, fn, label=label)

    def advance_clock(self, delta_ms: int) -> None:
        """Push the virtual clock forward by ``delta_ms`` (for slow-IO folding).

        The seams report simulated latency (a slow redis command, a slow disk op)
        and the wiring folds it back into time through here, so an op that "took"
        80ms actually advances the timeline 80ms — keeping cause and effect on the
        same clock without a real sleep.
        """
        if delta_ms > 0:
            self.clock.advance_to(self.clock.now_ms + delta_ms)

    def run_sync(self, coro: Coroutine[Any, Any, _T]) -> _T:
        """Run ``coro`` to completion *now*, at the current virtual instant.

        Valid only because simulation coroutines never truly suspend (see module
        docstring). If a coroutine ever did suspend on a real awaitable this would
        deadlock — which is the correct, loud failure: it would mean a non-virtual
        IO path leaked into the sim.
        """
        return self._aio.run_until_complete(coro)

    def run_resilient(
        self,
        make_coro: Callable[[], Coroutine[Any, Any, _T]],
        *,
        transient: tuple[type[BaseException], ...],
        attempts: int = 6,
        backoff_ms: int = 50,
        default: _T | None = None,
    ) -> _T | None:
        """Run a coroutine with retry-on-transient — mirroring production callers.

        The real control plane never calls the queue/scheduler bare: the API route
        wrapping ``on_event`` and the worker lane loop wrapping ``claim`` both catch
        transient broker errors and re-poll (``app.queue.worker`` lines 525-542:
        "never let one job kill the lane loop"). A faithful simulation must model
        that *outer resilience*, not just the inner operation — otherwise a single
        injected ``REDIS_ERROR`` would "crash" code that production would have
        retried, producing a false bug.

        So this retries ``make_coro()`` on the given ``transient`` exception types,
        advancing the virtual clock by ``backoff_ms`` between attempts (a real
        broker blip clears in milliseconds). After ``attempts`` failures it returns
        ``default`` — modelling the caller giving up on this tick and trying again
        on the next, exactly as the production loop does. ``make_coro`` is a factory
        because a coroutine can only be awaited once; each retry needs a fresh one.
        """
        last_exc: BaseException | None = None
        for i in range(attempts):
            try:
                return self._aio.run_until_complete(make_coro())
            except transient as exc:  # noqa: PERF203 - retry loop is the point
                last_exc = exc
                if i < attempts - 1:
                    self.advance_clock(backoff_ms)
        # Exhausted: the caller's tick gives up gracefully (returns default), the
        # same way a production lane loop drops this iteration and re-polls later.
        _ = last_exc
        return default

    def run_until_idle(self, *, max_steps: int = 5_000_000) -> int:
        """Drain the virtual event loop until no events remain."""
        return self.loop.run_until_idle(max_steps=max_steps)

    def run_until(self, deadline_ms: int, *, max_steps: int = 5_000_000) -> int:
        """Drain events up to ``deadline_ms`` (inclusive), then stop."""
        return self.loop.run_until(deadline_ms, max_steps=max_steps)

    # ----------------------------------------------------------------------- #
    # Lifecycle
    # ----------------------------------------------------------------------- #

    @property
    def buggify_log(self) -> BuggifyLog:
        """The trace of every fault that fired this run."""
        return self._buggify_log

    def close(self) -> None:
        """Release the private asyncio loop (if we created it)."""
        if self._owns_aio and not self._aio.is_closed():
            self._aio.close()

    def __enter__(self) -> Simulation:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


__all__ = ["Simulation"]
