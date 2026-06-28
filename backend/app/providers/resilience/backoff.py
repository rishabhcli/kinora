"""Retry backoff schedules with jitter (the §12.1 exponential-backoff retries).

The round-1 :class:`~app.providers.base.ProviderClient` already retries via
``tenacity``'s ``wait_exponential_jitter``. This module is the *gateway's* own,
fully-deterministic-under-an-injected-RNG backoff layer, used by
:class:`~app.providers.resilience.gateway.ResilientGateway` so the gateway can:

* honor a server-supplied ``Retry-After`` (a 429 / ``RateLimited`` carries
  ``retry_after_s``) — the single most effective backoff signal a provider gives;
* choose between the three classic jitter strategies (AWS's "Exponential Backoff
  and Jitter"): **full**, **equal**, and **decorrelated** jitter; and
* stay exhaustively testable by taking an injectable ``rng`` (``random.Random``)
  and never reading the wall clock.

Why a separate layer from tenacity: the gateway composes breakers + adaptive
rate-limiting + hedging + caching around a *single* attempt callable, and needs
the backoff to be a pure function of ``(attempt, last_delay, retry_after)`` so the
whole loop is reproducible in tests. tenacity stays in ``base`` for the transport.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class JitterStrategy(StrEnum):
    """The three canonical backoff-with-jitter strategies.

    * ``NONE`` — pure exponential, no randomness (deterministic; useful in tests).
    * ``FULL`` — ``uniform(0, exp_cap)``; best general-purpose spread.
    * ``EQUAL`` — ``exp_cap/2 + uniform(0, exp_cap/2)``; keeps a floor.
    * ``DECORRELATED`` — ``uniform(base, prev_delay*3)``; self-clocking, great
      under contention because each client walks its own random ladder.
    """

    NONE = "none"
    FULL = "full"
    EQUAL = "equal"
    DECORRELATED = "decorrelated"


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Tunables for :class:`BackoffSchedule` (deterministic; no env reads)."""

    base_s: float = 0.5
    max_s: float = 30.0
    multiplier: float = 2.0
    strategy: JitterStrategy = JitterStrategy.FULL
    #: When True, a server ``Retry-After`` (seconds) clamps the *floor* of the
    #: computed delay — we never wait *less* than the server asked, but jitter can
    #: still push us a little later to de-correlate a thundering herd.
    respect_retry_after: bool = True
    #: Hard ceiling applied after Retry-After is folded in, so a hostile/huge
    #: ``Retry-After`` cannot stall a render slot indefinitely.
    retry_after_cap_s: float = 120.0

    def __post_init__(self) -> None:
        if self.base_s <= 0:
            raise ValueError("base_s must be > 0")
        if self.max_s < self.base_s:
            raise ValueError("max_s must be >= base_s")
        if self.multiplier < 1.0:
            raise ValueError("multiplier must be >= 1.0")


class BackoffSchedule:
    """A stateful, reproducible backoff sequence for one retry loop.

    Construct one per logical operation (it carries the decorrelated-jitter
    state). ``next_delay`` returns the seconds to sleep before the *next* attempt;
    ``attempt`` is 1-based (the delay *after* the first failed attempt is
    ``next_delay(1)``).
    """

    def __init__(
        self, policy: BackoffPolicy | None = None, *, rng: random.Random | None = None
    ) -> None:
        self.policy = policy or BackoffPolicy()
        self._rng = rng or random.Random()
        self._prev_delay = self.policy.base_s

    def _exp_cap(self, attempt: int) -> float:
        """The exponential ceiling for a 1-based attempt, clamped to ``max_s``."""
        raw = self.policy.base_s * (self.policy.multiplier ** max(attempt - 1, 0))
        return min(raw, self.policy.max_s)

    def _jittered(self, attempt: int) -> float:
        cap = self._exp_cap(attempt)
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
        least that (capped at ``retry_after_cap_s``) — the server's hint wins, with
        jitter only ever *adding* slack on top of it.
        """
        delay = self._jittered(attempt)
        if retry_after_s is not None and self.policy.respect_retry_after:
            floor = min(retry_after_s, self.policy.retry_after_cap_s)
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
