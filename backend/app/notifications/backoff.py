"""Pure exponential-backoff retry policy (kinora.md §12.1).

Mirrors the render queue's retry philosophy (``RetryPolicy`` in
:mod:`app.queue.redis_queue`) but for notification/webhook delivery: a 1-based
attempt counter, a backoff schedule, a cap past which the work is dead-lettered,
and *full jitter* so a fleet of retrying workers does not synchronize into a
thundering herd against a flapping endpoint.

Everything here is deterministic given an injected ``rng`` (a ``random.Random``),
so the jitter is fully testable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import StrEnum


class RetryDecision(StrEnum):
    """What the policy decided for a just-failed attempt."""

    RETRY = "retry"
    DEADLETTER = "deadletter"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with a cap and optional full jitter.

    ``base_s`` * ``factor`` ** (attempt-1), clamped to ``max_delay_s``. ``attempt``
    is 1-based — the attempt that just failed. When ``attempt > max_attempts`` the
    work dead-letters. With ``jitter`` the returned delay is uniformly sampled in
    ``[0, computed]`` (AWS "full jitter"), which both spreads load and never
    *exceeds* the ceiling.
    """

    max_attempts: int = 5
    base_s: float = 2.0
    factor: float = 4.0
    max_delay_s: float = 300.0
    jitter: bool = True

    def decide(self, attempt: int) -> RetryDecision:
        """Retry while within the attempt cap; dead-letter past it."""
        return RetryDecision.DEADLETTER if attempt >= self.max_attempts else RetryDecision.RETRY

    def base_delay_for(self, attempt: int) -> float:
        """The (un-jittered) backoff ceiling for a 1-based ``attempt``."""
        if attempt < 1:
            attempt = 1
        raw = self.base_s * (self.factor ** (attempt - 1))
        return min(raw, self.max_delay_s)

    def delay_for(self, attempt: int, *, rng: random.Random | None = None) -> float:
        """The delay before re-attempting after a failed ``attempt`` (jittered)."""
        ceiling = self.base_delay_for(attempt)
        if not self.jitter or ceiling <= 0:
            return ceiling
        r = rng or random
        return r.uniform(0.0, ceiling)

    def schedule(self, *, rng: random.Random | None = None) -> list[float]:
        """The full per-attempt delay schedule (handy for tests + introspection)."""
        return [self.delay_for(a, rng=rng) for a in range(1, self.max_attempts)]


@dataclass(slots=True)
class RetryState:
    """Mutable retry bookkeeping for one in-flight delivery.

    The dispatcher / webhook engine threads this through attempts; it records the
    attempt count and the running list of errors so a dead-letter row captures the
    full failure history rather than only the last message.
    """

    attempts: int = 0
    errors: list[str] = field(default_factory=list)

    def record_failure(self, error: str) -> int:
        """Increment the attempt counter and append ``error``; return the new count."""
        self.attempts += 1
        self.errors.append(error[:500])
        return self.attempts

    @property
    def last_error(self) -> str | None:
        return self.errors[-1] if self.errors else None


__all__ = ["RetryDecision", "RetryPolicy", "RetryState"]
