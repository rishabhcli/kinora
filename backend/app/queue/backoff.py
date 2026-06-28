"""Exponential-backoff schedules with jitter for the render-retry path (§12.1).

kinora.md §12.1 calls for *exponential-backoff retries (e.g., 2s, 8s, 30s) for
transient DashScope failures*. The queue's :class:`~app.queue.redis_queue.RetryPolicy`
takes a **fixed** per-attempt schedule; that is deterministic and easy to reason
about, but when many shots fail at once (a DashScope blip, a rate-quota wall)
fixed delays make every worker retry on the *same* tick — a thundering herd that
re-hammers the provider in lockstep.

This module adds **jitter**: each retry's delay is randomised within a bounded
window so retries spread out, while the *expected* delay still grows
exponentially. It is decoupled from Redis so the maths is pure and unit-testable.

Three industry-standard strategies (after the AWS "Exponential Backoff And Jitter"
guidance), plus the queue's existing fixed schedule as a strategy:

* **full jitter** — ``delay = uniform(0, min(cap, base * 2**n))``. Maximum spread;
  the default for the render queue (provider calls are independent + idempotent).
* **equal jitter** — ``delay = half + uniform(0, half)`` where ``half`` is the
  exponential term / 2. Keeps a guaranteed floor while still spreading.
* **decorrelated jitter** — ``delay = min(cap, uniform(base, prev * 3))``. Stateful;
  the smoothest provider load profile under sustained failure.
* **fixed** — the literal ``(2, 8, 30)`` schedule, jitter-free (back-compat).

A :class:`BackoffSchedule` snapshots a strategy + RNG so a test can pin the seed
and get exact delays; :meth:`materialise` precomputes a fixed tuple that drops
straight into :class:`RetryPolicy` for callers that want a deterministic schedule.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "JitterStrategy",
    "BackoffSchedule",
    "DEFAULT_BASE_S",
    "DEFAULT_CAP_S",
    "full_jitter",
    "equal_jitter",
    "decorrelated_jitter",
]

#: The queue's canonical exponential base + cap (§12.1's "2s, 8s, 30s" shape).
DEFAULT_BASE_S = 2.0
DEFAULT_CAP_S = 30.0


class JitterStrategy(StrEnum):
    """How a backoff delay is randomised around its exponential term."""

    NONE = "none"
    FULL = "full"
    EQUAL = "equal"
    DECORRELATED = "decorrelated"


def _exp_term(attempt: int, base: float, cap: float) -> float:
    """The capped exponential term for a 1-based ``attempt`` (``base * 2**(n-1)``)."""
    if attempt < 1:
        attempt = 1
    # 2**(n-1) so attempt 1 -> base, attempt 2 -> 2*base, attempt 3 -> 4*base …
    raw = base * (2.0 ** (attempt - 1))
    return min(cap, raw)


def full_jitter(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    """``uniform(0, min(cap, base * 2**(n-1)))`` — maximum spread (AWS "full jitter")."""
    return rng.uniform(0.0, _exp_term(attempt, base, cap))


def equal_jitter(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    """``half + uniform(0, half)`` — a guaranteed floor plus spread."""
    half = _exp_term(attempt, base, cap) / 2.0
    return half + rng.uniform(0.0, half)


def decorrelated_jitter(
    attempt: int, *, base: float, cap: float, prev: float, rng: random.Random
) -> float:
    """``min(cap, uniform(base, prev * 3))`` — stateful, smoothest under load.

    ``prev`` is the *previous* delay (``base`` for the first attempt).
    """
    floor = base
    ceil = max(base, prev * 3.0)
    return min(cap, rng.uniform(floor, ceil))


@dataclass(slots=True)
class BackoffSchedule:
    """A reusable exponential-backoff schedule with bounded jitter.

    ``delay_for(attempt)`` returns the (clamped, jittered) seconds to wait before
    the 1-based ``attempt`` retry. Decorrelated jitter is stateful, so a schedule
    instance tracks the last delay; call :meth:`reset` to start a fresh job. For a
    deterministic, jitter-free schedule pass ``strategy=NONE`` with an explicit
    ``fixed`` tuple (the queue's back-compat path).
    """

    strategy: JitterStrategy = JitterStrategy.FULL
    base_s: float = DEFAULT_BASE_S
    cap_s: float = DEFAULT_CAP_S
    fixed: tuple[float, ...] = ()
    seed: int | None = None
    _rng: random.Random = field(init=False, repr=False, compare=False)
    _prev: float = field(init=False, repr=False, compare=False, default=DEFAULT_BASE_S)

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be positive")
        if self.cap_s < self.base_s:
            raise ValueError("cap_s must be >= base_s")
        self._rng = random.Random(self.seed)
        self._prev = self.base_s

    def reset(self) -> None:
        """Reset the jitter state for a new job/trajectory.

        Re-seeds the RNG (so a seeded schedule replays identically) and clears the
        decorrelated-jitter ``prev`` accumulator.
        """
        self._rng = random.Random(self.seed)
        self._prev = self.base_s

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before the 1-based ``attempt``-th retry."""
        if self.strategy is JitterStrategy.NONE:
            return self._fixed_delay(attempt)
        if self.strategy is JitterStrategy.FULL:
            delay = full_jitter(attempt, base=self.base_s, cap=self.cap_s, rng=self._rng)
        elif self.strategy is JitterStrategy.EQUAL:
            delay = equal_jitter(attempt, base=self.base_s, cap=self.cap_s, rng=self._rng)
        else:  # DECORRELATED
            delay = decorrelated_jitter(
                attempt, base=self.base_s, cap=self.cap_s, prev=self._prev, rng=self._rng
            )
            self._prev = delay
        return round(min(self.cap_s, delay), 3)

    def _fixed_delay(self, attempt: int) -> float:
        if not self.fixed:
            # No literal schedule given: fall back to the pure capped exponential.
            return _exp_term(attempt, self.base_s, self.cap_s)
        idx = max(attempt - 1, 0)
        return self.fixed[min(idx, len(self.fixed) - 1)]

    def materialise(self, attempts: int) -> tuple[float, ...]:
        """Precompute the first ``attempts`` delays as a fixed tuple.

        Handy to hand a *deterministic* (seeded) jittered schedule to
        :class:`RetryPolicy`, whose ``backoff_for`` indexes a fixed tuple. The
        schedule is reset first so the result is reproducible for a given seed.
        """
        self.reset()
        return tuple(self.delay_for(n) for n in range(1, max(attempts, 0) + 1))

    @classmethod
    def fixed_schedule(cls, schedule: Sequence[float]) -> BackoffSchedule:
        """A jitter-free schedule from a literal ``(2, 8, 30)``-style tuple."""
        return cls(strategy=JitterStrategy.NONE, fixed=tuple(schedule))
