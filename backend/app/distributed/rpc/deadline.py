"""Deadlines + the injectable clock — the time budget that rides every call.

A deadline is the single most important cross-service primitive: it bounds how
long a call may take *end to end*, and — crucially — it **propagates**. When
service A (deadline 2 s) calls service B, B inherits *A's remaining* budget, not
a fresh 2 s, so a chain can never collectively exceed the originator's bound.
This is how a reader's seek (§4.8) can cancel an in-flight render chain: the
deadline shrinks as the work moves down the chain and trips ``DEADLINE_EXCEEDED``
the moment the budget is spent.

Everything here is driven by an injectable :class:`Clock` (monotonic seconds) so
tests advance time deterministically with a :class:`ManualClock` — no real
``sleep`` ever runs in the suite. Deadlines are stored as **absolute monotonic
instants** (not durations) so they compare correctly regardless of when they are
read.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """A monotonic seconds clock. ``now()`` must be non-decreasing."""

    def now(self) -> float:
        """Return the current monotonic time in seconds."""
        ...


class SystemClock:
    """The production clock: :func:`time.monotonic`."""

    __slots__ = ()

    def now(self) -> float:
        """Return ``time.monotonic()``."""
        return time.monotonic()


@dataclass
class ManualClock:
    """A deterministic virtual clock for tests (advance it explicitly).

    Time only moves when a test calls :meth:`advance` / :meth:`set`, so a
    deadline / backoff / hedge delay never waits in wall-clock time — the test
    drives every temporal decision. This is what keeps the suite fast *and*
    free of flakiness around timing.
    """

    _t: float = 0.0

    def now(self) -> float:
        """Return the current virtual time."""
        return self._t

    def advance(self, seconds: float) -> float:
        """Advance virtual time by ``seconds`` (non-negative) and return now."""
        if seconds < 0:
            raise ValueError("cannot advance time backwards")
        self._t += seconds
        return self._t

    def set(self, t: float) -> None:
        """Set virtual time to an absolute value (must not go backwards)."""
        if t < self._t:
            raise ValueError("cannot set time backwards")
        self._t = t


@dataclass(frozen=True, slots=True)
class Deadline:
    """An absolute monotonic instant after which a call must stop.

    Construct relative to *now* with :meth:`after`; never expire with
    :meth:`never`. :meth:`remaining` is what gets propagated to a downstream
    call, and :meth:`min_with` takes the *tighter* of two deadlines so an
    inherited budget can only shrink.
    """

    #: Absolute monotonic instant (seconds). ``inf`` means "no deadline".
    at: float

    @classmethod
    def after(cls, seconds: float, *, clock: Clock) -> Deadline:
        """A deadline ``seconds`` from now on ``clock``.

        A non-positive ``seconds`` yields an already-expired deadline at *now*
        (the call has no budget), not an infinite one — use :meth:`never` for
        "unbounded". ``inf`` seconds yields an infinite deadline.
        """
        if seconds == float("inf"):
            return cls(at=float("inf"))
        if seconds <= 0:
            return cls(at=clock.now())
        return cls(at=clock.now() + seconds)

    @classmethod
    def never(cls) -> Deadline:
        """A deadline that never expires."""
        return cls(at=float("inf"))

    @classmethod
    def at_instant(cls, instant: float) -> Deadline:
        """A deadline at an explicit absolute monotonic instant."""
        return cls(at=instant)

    @property
    def is_infinite(self) -> bool:
        """True when this deadline never expires."""
        return self.at == float("inf")

    def remaining(self, *, clock: Clock) -> float:
        """Seconds left before expiry (``0.0`` once expired, ``inf`` if never)."""
        if self.is_infinite:
            return float("inf")
        return max(0.0, self.at - clock.now())

    def expired(self, *, clock: Clock) -> bool:
        """True once the deadline instant has passed."""
        if self.is_infinite:
            return False
        return clock.now() >= self.at

    def min_with(self, other: Deadline) -> Deadline:
        """Return the tighter (earlier) of two deadlines.

        Used to inherit a budget: a downstream call takes ``min`` of the
        caller's remaining deadline and any per-hop bound, so a chain's total
        time can never exceed the originator's deadline.
        """
        return self if self.at <= other.at else other


def deadline_for(
    timeout_s: float | None,
    *,
    clock: Clock,
    inherited: Deadline | None = None,
) -> Deadline:
    """Compute the effective deadline for a call.

    Combines an optional per-call ``timeout_s`` with an optional ``inherited``
    deadline (from the caller's context), taking the tighter of the two so the
    budget only ever shrinks down a call chain. With neither, the deadline is
    infinite (the call is unbounded — used by background work).
    """
    own = Deadline.after(timeout_s, clock=clock) if timeout_s is not None else Deadline.never()
    if inherited is None:
        return own
    return own.min_with(inherited)


__all__ = [
    "Clock",
    "Deadline",
    "ManualClock",
    "SystemClock",
    "deadline_for",
]
