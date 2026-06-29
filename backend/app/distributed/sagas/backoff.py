"""Retry policy + backoff schedule for saga steps.

A pure, side-effect-free decision object: given the number of attempts a step has
already made, decide whether to retry the step or give up (which, for a forward
step, means compensate; for a compensation, means a fatal ``COMPENSATION_FAILED``).
It mirrors :class:`app.jobs.backoff.BackoffPolicy` and the render queue's
``RetryPolicy`` so the whole backend retries with one shape — exponential
``base * factor**(attempt-1)`` clamped to ``max_delay``, with optional full
jitter drawn from an injected :class:`random.Random` for deterministic tests.

Sagas additionally distinguish the *forward* retry budget from the *compensation*
retry budget: a compensation that keeps failing is far more dangerous than a
forward step that does (the system is now stuck with a half-applied effect), so
the engine gives compensations their own, typically larger, budget.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from enum import StrEnum


class RetryDecision(StrEnum):
    """What :meth:`BackoffPolicy.decide` resolved to."""

    RETRY = "retry"
    GIVE_UP = "give_up"  # forward → compensate; compensation → fatal


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with a cap, optional jitter, and a retry ceiling.

    ``max_attempts`` is the total number of attempts allowed (1 == no retry). An
    attempt count *at or beyond* ``max_attempts`` gives up. ``delay_for`` is keyed
    on the *upcoming* attempt number (1-based): the delay before attempt 2 is
    ``delay_for(2)``; the delay before the first attempt is always 0.
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
        """Retry while ``attempts_made < max_attempts``, else give up."""
        if attempts_made < self.max_attempts:
            return RetryDecision.RETRY
        return RetryDecision.GIVE_UP

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


#: Forward steps: a tight (2s, 8s) ladder mirroring the render queue shape.
DEFAULT_FORWARD_POLICY = BackoffPolicy(
    max_attempts=3, base_delay_s=2.0, factor=4.0, max_delay_s=300.0
)

#: Compensations: a more generous budget — abandoning a compensation strands a
#: half-applied effect, so we try harder before declaring a fatal failure.
DEFAULT_COMPENSATION_POLICY = BackoffPolicy(
    max_attempts=5, base_delay_s=2.0, factor=3.0, max_delay_s=300.0
)


__all__ = [
    "DEFAULT_COMPENSATION_POLICY",
    "DEFAULT_FORWARD_POLICY",
    "BackoffPolicy",
    "RetryDecision",
]
