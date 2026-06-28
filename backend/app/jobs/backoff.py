"""Retry policy + backoff schedule for the jobs framework.

A pure, side-effect-free decision object (mirrors the render queue's
:class:`app.queue.redis_queue.RetryPolicy`, but parameterised for *operational*
jobs): given the number of attempts already made, decide whether to retry or
dead-letter, and compute the (jittered, capped) delay before the next attempt.

The backoff is exponential — ``base * factor**(attempt-1)`` — clamped to
``max_delay``, with optional *full jitter* (a deterministic-when-seeded uniform
sample in ``[0, delay]``) so a fleet retrying together doesn't thunder. Jitter
draws from an injected :class:`random.Random` so tests can seed it for exact
assertions; production passes the module-global RNG.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class RetryDecision(StrEnum):
    """What :meth:`BackoffPolicy.decide` resolved to."""

    RETRY = "retry"
    DEADLETTER = "deadletter"


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with a cap, optional jitter, and a retry ceiling.

    ``max_attempts`` is the total number of attempts allowed (1 == no retry). An
    attempt count *greater than* ``max_attempts`` dead-letters. ``delay_for`` is
    keyed on the *upcoming* attempt number (1-based): the delay before attempt 2
    is ``delay_for(2)``.
    """

    max_attempts: int = 3
    base_delay_s: float = 2.0
    factor: float = 4.0
    max_delay_s: float = 300.0
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("delays must be non-negative")
        if self.factor < 1.0:
            raise ValueError("factor must be >= 1.0")

    def decide(self, attempts_made: int) -> RetryDecision:
        """Retry while ``attempts_made < max_attempts``, else dead-letter."""
        if attempts_made < self.max_attempts:
            return RetryDecision.RETRY
        return RetryDecision.DEADLETTER

    def raw_delay_for(self, next_attempt: int) -> float:
        """The un-jittered, capped delay before ``next_attempt`` (1-based)."""
        if next_attempt <= 1:
            return 0.0
        exp = self.base_delay_s * (self.factor ** (next_attempt - 2))
        return min(exp, self.max_delay_s)

    def delay_for(self, next_attempt: int, *, rng: random.Random | None = None) -> float:
        """The (optionally jittered) delay in seconds before ``next_attempt``."""
        delay = self.raw_delay_for(next_attempt)
        if not self.jitter or delay <= 0:
            return delay
        r = rng if rng is not None else random
        return r.uniform(0.0, delay)


#: A sensible default mirroring the render queue's (2s, 8s, 30s) schedule shape.
DEFAULT_POLICY = BackoffPolicy(max_attempts=3, base_delay_s=2.0, factor=4.0, max_delay_s=300.0)


__all__ = ["DEFAULT_POLICY", "BackoffPolicy", "RetryDecision"]
