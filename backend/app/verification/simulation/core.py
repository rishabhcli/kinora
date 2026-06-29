"""The deterministic engine: a seeded PRNG, a virtual clock, and a single-threaded
discrete-event scheduler (kinora.md §12 — "built like it" for flaky async work).

This is the FoundationDB heart of the simulator. Everything reproducible in the
framework bottoms out here, on three guarantees:

#. **One source of randomness.** :class:`Prng` is a splittable, fully seeded PRNG.
   No subsystem may call :mod:`random`, ``time``, ``uuid``, or the OS — they draw
   from a :class:`Prng` handed down from the root seed. Same seed → same bytes,
   forever, on any machine.
#. **One source of time.** :class:`SimClock` advances *only* when the event loop
   says so (when it pops the next scheduled event). Wall-clock never enters the
   simulation; "100ms of network latency" is an integer added to a virtual
   timeline, not a real sleep. This is what lets thousands of fault schedules run
   in milliseconds.
#. **One thread of control.** :class:`EventLoop` is a min-heap of timed callbacks
   drained in a strict, deterministic order: earlier time first, ties broken by a
   monotonically increasing sequence number (FIFO within an instant). There is no
   OS scheduler, no preemption, no race — concurrency is *modelled* as interleaved
   events, and the interleaving is a function of the seed alone.

Why a custom loop and not :mod:`asyncio`? Because asyncio's ordering is not
deterministic across runs (task wakeups depend on the OS), and its clock is real.
A FoundationDB-style simulator must own scheduling and time to be reproducible;
those are exactly the two things asyncio refuses to give up. The cost is that
simulated subsystems are written against *our* seams (the :class:`SimClock`
``now_ms`` and the :class:`EventLoop` ``call_at``), not ``asyncio.sleep`` — which
is the whole point: the real :class:`~app.scheduler.service.SchedulerService` and
:class:`~app.queue.redis_queue.RedisRenderQueue` already accept injectable
``now_ms`` / ``clock_ms`` callables, so they drop straight in (see
:mod:`app.verification.simulation.system`).

The loop is *synchronous* on purpose: simulated coroutines are expressed as
callbacks that re-schedule themselves, not as ``async def``. Driving the real
``async`` services is handled by :class:`~app.verification.simulation.runtime.Simulation`,
which pumps a private asyncio loop one step at a time against this virtual clock.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: A callback scheduled on the loop. Receives the current sim time (ms) so a
#: handler never needs to read a clock itself.
EventCallback = Callable[[int], None]

# Golden-ratio odd constant for SplitMix64 (Steele et al.). Mixes a counter into
# a well-distributed 64-bit stream with no warm-up.
_SPLITMIX_GAMMA = 0x9E3779B97F4A7C15
_MASK64 = (1 << 64) - 1


def _splitmix64(state: int) -> tuple[int, int]:
    """One SplitMix64 step: returns ``(next_state, output)`` (both 64-bit).

    SplitMix64 is the canonical seeding/splitting generator (it is what seeds
    xoshiro). It is trivially portable — pure integer ops, no platform float —
    which is exactly what a cross-machine-reproducible simulator needs.
    """
    state = (state + _SPLITMIX_GAMMA) & _MASK64
    z = state
    z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK64
    z = z ^ (z >> 31)
    return state, z


class Prng:
    """A deterministic, splittable pseudo-random source (SplitMix64).

    Every stochastic decision in the simulation — fault rolls, network latency
    draws, reader jitter, id generation — pulls from a :class:`Prng`. The root
    PRNG is created from the run seed; subsystems get *independent* streams via
    :meth:`split` so adding a draw in one subsystem cannot shift the byte stream
    another subsystem sees (the classic "I added a log line and all my seeds
    changed" failure). Splitting is how FoundationDB keeps fault schedules stable
    as the code under test evolves.

    Not cryptographic. Reproducibility and speed are the only goals.
    """

    __slots__ = ("_state",)

    def __init__(self, seed: int) -> None:
        self._state = seed & _MASK64

    def _next_u64(self) -> int:
        self._state, out = _splitmix64(self._state)
        return out

    def split(self, label: str = "") -> Prng:
        """Return a fresh, independent PRNG derived from this one.

        ``label`` lets named subsystems take stable streams: ``split("network")``
        and ``split("disk")`` always diverge, and re-ordering the splits does not
        cross-contaminate because each child is seeded from a fresh draw mixed
        with the label's hash. The parent stream advances by exactly one draw.
        """
        child_seed = self._next_u64()
        if label:
            # Fold a stable label hash in so two same-position splits with
            # different labels diverge (FNV-1a 64-bit over the bytes).
            h = 0xCBF29CE484222325
            for b in label.encode("utf-8"):
                h = ((h ^ b) * 0x100000001B3) & _MASK64
            child_seed ^= h
        return Prng(child_seed)

    def random(self) -> float:
        """A float in ``[0.0, 1.0)`` with 53 bits of entropy (IEEE-754 safe)."""
        return (self._next_u64() >> 11) * (1.0 / (1 << 53))

    def randint(self, low: int, high: int) -> int:
        """A uniform integer in the inclusive range ``[low, high]``."""
        if high < low:
            raise ValueError(f"randint range empty: [{low}, {high}]")
        span = high - low + 1
        return low + int(self._next_u64() % span)

    def chance(self, probability: float) -> bool:
        """``True`` with the given probability — the core Buggify coin flip."""
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        return self.random() < probability

    def choice(self, items: list[Any]) -> Any:
        """Pick one element uniformly at random; raises on empty input."""
        if not items:
            raise IndexError("cannot choose from an empty sequence")
        return items[self.randint(0, len(items) - 1)]

    def uniform(self, low: float, high: float) -> float:
        """A float drawn uniformly from ``[low, high]``."""
        if high < low:
            low, high = high, low
        return low + (high - low) * self.random()

    def jitter(self, base: float, fraction: float) -> float:
        """``base`` perturbed by ±``fraction`` of itself (clamped non-negative)."""
        delta = base * fraction
        return max(0.0, self.uniform(base - delta, base + delta))

    def hexid(self, prefix: str, nbytes: int = 8) -> str:
        """A deterministic hex id like ``shot_4f1a...`` — replaces ``uuid4()``.

        Because ids are drawn from the seeded stream, two runs of the same seed
        produce identical ids, so logs and event traces line up byte-for-byte.
        """
        value = self._next_u64()
        width = max(1, nbytes * 2)
        return f"{prefix}_{value & ((1 << (nbytes * 8)) - 1):0{width}x}"


class SimClock:
    """The simulation's only clock — virtual milliseconds since an epoch of 0.

    Reads (:attr:`now_ms`) are free and never advance time. Time advances solely
    through :meth:`advance_to`, which the :class:`EventLoop` calls as it pops the
    next event. Monotonic by construction: it refuses to move backward, so any
    subsystem keying off "time only goes forward" (lease expiry, idle-pause,
    backoff) sees production semantics with zero wall-clock cost.
    """

    __slots__ = ("_now_ms",)

    def __init__(self, start_ms: int = 0) -> None:
        self._now_ms = int(start_ms)

    @property
    def now_ms(self) -> int:
        """Current virtual time in integer milliseconds."""
        return self._now_ms

    @property
    def now_s(self) -> float:
        """Current virtual time in seconds (for second-based seams)."""
        return self._now_ms / 1000.0

    def advance_to(self, t_ms: int) -> None:
        """Jump the clock forward to ``t_ms``; never backward (monotonic)."""
        if t_ms > self._now_ms:
            self._now_ms = int(t_ms)

    def as_callable_ms(self) -> Callable[[], int]:
        """A ``() -> int`` view for injecting into real ``clock_ms`` seams.

        :class:`~app.queue.redis_queue.RedisRenderQueue` and the scheduler's
        :class:`~app.scheduler.intent.Intent` both accept a ``clock_ms`` callable;
        this hands them the virtual clock so they advance with the sim.
        """
        return lambda: self._now_ms

    def as_callable_s(self) -> Callable[[], float]:
        """A ``() -> float`` monotonic view for ``time.monotonic``-style seams.

        The autoscaler (:class:`~app.queue.autoscale.AutoscaleState`) keys off a
        monotonic-seconds clock; this satisfies it deterministically.
        """
        return lambda: self._now_ms / 1000.0


@dataclass(order=True)
class _ScheduledEvent:
    """One entry in the event heap. Ordered by ``(time, seq)`` for determinism."""

    t_ms: int
    seq: int
    callback: EventCallback = field(compare=False)
    label: str = field(default="", compare=False)
    cancelled: bool = field(default=False, compare=False)


@dataclass(frozen=True, slots=True)
class EventHandle:
    """A cancellable reference to a scheduled event (for timer cancellation)."""

    _event: _ScheduledEvent

    @property
    def fire_at_ms(self) -> int:
        """The virtual time at which this event is scheduled to fire."""
        return self._event.t_ms

    @property
    def cancelled(self) -> bool:
        """Whether this event has been cancelled (and will be skipped)."""
        return self._event.cancelled

    def cancel(self) -> None:
        """Prevent the event from firing (a no-op if it already fired)."""
        self._event.cancelled = True


class EventLoop:
    """A deterministic, single-threaded discrete-event scheduler.

    A min-heap of timed callbacks, drained in strict ``(time, seq)`` order. The
    ``seq`` tiebreaker is a monotonically increasing counter assigned at schedule
    time, so events scheduled for the same instant fire in the order they were
    scheduled (FIFO within an instant) — no hidden nondeterminism. Wall-clock is
    never consulted; the loop simply advances :class:`SimClock` to each event's
    time as it pops it.

    The loop owns the clock so "advance time" and "run due work" are the same
    operation, which is what makes the whole run a pure function of the seed and
    the scheduled events.
    """

    __slots__ = ("_clock", "_heap", "_seq", "_steps")

    def __init__(self, clock: SimClock) -> None:
        self._clock = clock
        self._heap: list[_ScheduledEvent] = []
        self._seq = 0
        self._steps = 0

    @property
    def clock(self) -> SimClock:
        """The clock this loop drives."""
        return self._clock

    @property
    def steps(self) -> int:
        """How many events have fired so far (a determinism fingerprint)."""
        return self._steps

    @property
    def pending(self) -> int:
        """Number of not-yet-fired events still on the heap (incl. cancelled)."""
        return len(self._heap)

    def call_at(self, t_ms: int, callback: EventCallback, *, label: str = "") -> EventHandle:
        """Schedule ``callback`` to run at absolute virtual time ``t_ms``.

        ``t_ms`` is clamped to never precede the current clock (you cannot
        schedule into the past). Returns a cancellable :class:`EventHandle`.
        """
        fire_at = max(int(t_ms), self._clock.now_ms)
        event = _ScheduledEvent(t_ms=fire_at, seq=self._seq, callback=callback, label=label)
        self._seq += 1
        heapq.heappush(self._heap, event)
        return EventHandle(event)

    def call_after(self, delay_ms: int, callback: EventCallback, *, label: str = "") -> EventHandle:
        """Schedule ``callback`` ``delay_ms`` from now (delay clamped to ≥ 0)."""
        return self.call_at(self._clock.now_ms + max(0, int(delay_ms)), callback, label=label)

    def step(self) -> bool:
        """Fire the single next due event. Returns ``False`` if the heap is empty.

        Cancelled events are popped and skipped without advancing time past
        their slot beyond the natural clock motion. This is the loop's atom: the
        whole run is a sequence of :meth:`step` calls.
        """
        while self._heap:
            event = heapq.heappop(self._heap)
            if event.cancelled:
                continue
            self._clock.advance_to(event.t_ms)
            self._steps += 1
            event.callback(self._clock.now_ms)
            return True
        return False

    def run_until_idle(self, *, max_steps: int = 5_000_000) -> int:
        """Drain the loop until no events remain (or ``max_steps`` is hit).

        Returns the number of events fired. ``max_steps`` is a safety valve: a
        livelock (a handler that endlessly reschedules itself at the same instant)
        would otherwise spin forever; hitting the cap raises so the offending seed
        surfaces instead of hanging the suite.
        """
        fired = 0
        while self.step():
            fired += 1
            if fired >= max_steps:
                raise RuntimeError(
                    f"event loop exceeded {max_steps} steps at t={self._clock.now_ms}ms "
                    "— suspected livelock (a handler rescheduling without advancing time)"
                )
        return fired

    def run_until(self, deadline_ms: int, *, max_steps: int = 5_000_000) -> int:
        """Drain events with fire time ≤ ``deadline_ms``, then stop.

        Events past the deadline stay on the heap. The clock ends at
        ``deadline_ms`` (so a subsequent poller sees the full elapsed window even
        if the last event fired earlier). Returns the number of events fired.
        """
        fired = 0
        while self._heap:
            nxt = self._heap[0]
            if nxt.cancelled:
                heapq.heappop(self._heap)
                continue
            if nxt.t_ms > deadline_ms:
                break
            self.step()
            fired += 1
            if fired >= max_steps:
                raise RuntimeError(
                    f"event loop exceeded {max_steps} steps at t={self._clock.now_ms}ms"
                )
        self._clock.advance_to(deadline_ms)
        return fired


__all__ = [
    "EventCallback",
    "EventHandle",
    "EventLoop",
    "Prng",
    "SimClock",
]
