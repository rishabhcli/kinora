"""Exponential backoff with jitter — a pure function of ``(attempt, prev, hint)``.

The retry loop ([app.resilience.retry][]) computes the delay before each retry
through a :class:`BackoffSchedule`. Keeping the math here, behind an injected
``random.Random``, makes the whole loop reproducible: seed the RNG and the exact
sequence of (jittered) delays is fixed, so a test can assert tight bounds on every
backoff without ever sleeping.

The four strategies follow AWS's "Exponential Backoff And Jitter":

* ``NONE`` — pure exponential, deterministic. Useful when you *want* a fixed ladder.
* ``FULL`` — ``uniform(0, cap)``. The default; best general-purpose spread, the
  strongest de-correlator of a thundering herd.
* ``EQUAL`` — ``cap/2 + uniform(0, cap/2)``. Keeps a non-zero floor (won't hammer).
* ``DECORRELATED`` — ``uniform(base, prev*3)``. Self-clocking; each client walks its
  own random ladder, which spreads load best under sustained contention.

A server ``Retry-After`` (carried on a :class:`~app.resilience.errors.RateLimitedError`)
is folded in as a *floor* — we never wait *less* than the server asked, but jitter
may add slack on top — and then clamped so a hostile/huge hint can't stall forever.
This mirrors the provider-layer schedule so the two behave identically.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class JitterStrategy(StrEnum):
    """The four canonical backoff-with-jitter strategies (see module docstring)."""

    NONE = "none"
    FULL = "full"
    EQUAL = "equal"
    DECORRELATED = "decorrelated"


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Tunables for :class:`BackoffSchedule` (deterministic; no env reads)."""

    base_s: float = 0.1
    max_s: float = 30.0
    multiplier: float = 2.0
    strategy: JitterStrategy = JitterStrategy.FULL
    #: When True, a server ``Retry-After`` clamps the *floor* of the delay.
    respect_retry_after: bool = True
    #: Hard ceiling applied after Retry-After is folded in, so a hostile/huge hint
    #: cannot stall a slot indefinitely.
    retry_after_cap_s: float = 120.0

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be > 0")
        if self.max_s < self.base_s:
            raise ValueError("max_s must be >= base_s")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")
        if self.retry_after_cap_s < 0:
            raise ValueError("retry_after_cap_s must be >= 0")


class BackoffSchedule:
    """A stateful, reproducible backoff sequence for one retry loop.

    Construct one per logical operation (it carries the decorrelated-jitter state).
    :meth:`next_delay` returns the seconds to sleep before the *next* attempt;
    ``attempt`` is 1-based — the delay *after* the first failed attempt is
    ``next_delay(1)``.
    """

    def __init__(
        self, policy: BackoffPolicy | None = None, *, rng: random.Random | None = None
    ) -> None:
        self.policy = policy or BackoffPolicy()
        self._rng = rng or random.Random()
        self._prev_delay = self.policy.base_s

    def exp_cap(self, attempt: int) -> float:
        """The exponential ceiling for a 1-based attempt, clamped to ``max_s``.

        Public so a test can assert the pre-jitter envelope directly.
        """
        raw = self.policy.base_s * (self.policy.multiplier ** max(attempt - 1, 0))
        return min(raw, self.policy.max_s)

    def _jittered(self, attempt: int) -> float:
        cap = self.exp_cap(attempt)
        strategy = self.policy.strategy
        if strategy is JitterStrategy.NONE:
            delay = cap
        elif strategy is JitterStrategy.FULL:
            delay = self._rng.uniform(0.0, cap)
        elif strategy is JitterStrategy.EQUAL:
            half = cap / 2.0
            delay = half + self._rng.uniform(0.0, half)
        else:  # DECORRELATED
            upper = min(self._prev_delay * 3.0, self.policy.max_s)
            low = self.policy.base_s
            delay = self._rng.uniform(low, max(low, upper))
        self._prev_delay = max(delay, self.policy.base_s)
        return delay

    def next_delay(self, attempt: int, *, retry_after_s: float | None = None) -> float:
        """Seconds to wait before attempt ``attempt + 1`` (``attempt`` is 1-based).

        If ``retry_after_s`` is given and the policy respects it, the result is at
        least that (clamped at ``retry_after_cap_s``) — the server's hint wins, with
        jitter only ever *adding* slack on top of it. The result is always >= 0.
        """
        delay = self._jittered(attempt)
        if retry_after_s is not None and self.policy.respect_retry_after:
            floor = min(max(retry_after_s, 0.0), self.policy.retry_after_cap_s)
            delay = max(delay, floor)
        return max(delay, 0.0)

    def reset(self) -> None:
        """Reset the decorrelated-jitter walk (call when reusing a schedule)."""
        self._prev_delay = self.policy.base_s


__all__ = [
    "BackoffPolicy",
    "BackoffSchedule",
    "JitterStrategy",
]
