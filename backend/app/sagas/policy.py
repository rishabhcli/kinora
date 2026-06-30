"""Retry + timeout policy for saga steps — pure, deterministic functions.

A step's resilience is described declaratively so the engine's control flow has
no inline magic numbers and every decision is unit-testable in isolation:

* :class:`RetryPolicy` — how many attempts, and a **deterministic** exponential
  backoff schedule (optionally with bounded, *seeded* jitter so a flood of
  retries doesn't thunder, while staying reproducible in tests).
* :class:`TimeoutPolicy` — a per-attempt deadline (the action is raced against a
  timer) and an optional whole-step deadline across all attempts.

Backoff is expressed as *seconds to wait* and consumed by the engine through the
injected :class:`~app.sagas.clock.Clock` and a sleeper — never ``time.sleep`` —
so tests advance a :class:`~app.sagas.clock.FakeClock` instead of waiting.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A deterministic retry schedule for a step.

    Attributes:
        max_attempts: total attempts including the first (``1`` = no retry).
        base_backoff_s: wait before the *second* attempt (first retry).
        factor: multiplier applied each subsequent retry (exponential).
        max_backoff_s: ceiling so a long chain can't wait absurdly long.
        jitter_ratio: fraction (0..1) of the computed backoff used as a
            deterministic, key-seeded jitter band; ``0`` disables jitter.
    """

    max_attempts: int = 3
    base_backoff_s: float = 1.0
    factor: float = 2.0
    max_backoff_s: float = 60.0
    jitter_ratio: float = 0.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_backoff_s < 0 or self.max_backoff_s < 0:
            raise ValueError("backoff seconds must be >= 0")
        if not 0.0 <= self.jitter_ratio <= 1.0:
            raise ValueError("jitter_ratio must be in [0, 1]")

    @property
    def max_retries(self) -> int:
        """Retries after the initial attempt."""
        return self.max_attempts - 1

    def should_retry(self, attempt: int) -> bool:
        """True iff a *next* attempt is permitted after ``attempt`` (1-based)."""
        return attempt < self.max_attempts

    def backoff_for(self, attempt: int, *, seed: str = "") -> float:
        """Seconds to wait before retry following ``attempt`` (1-based).

        Exponential: ``base * factor**(attempt-1)``, capped at ``max_backoff_s``.
        When ``jitter_ratio > 0`` a *deterministic* offset in
        ``[-band, +band]`` (``band = jitter_ratio * raw``) derived from
        ``(seed, attempt)`` is applied — reproducible across processes, so tests
        get a fixed value, never ``random``.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        raw = self.base_backoff_s * (self.factor ** (attempt - 1))
        raw = min(raw, self.max_backoff_s)
        if self.jitter_ratio > 0.0:
            band = self.jitter_ratio * raw
            frac = _seeded_unit(f"{seed}:{attempt}")  # 0..1
            raw = raw + (2.0 * frac - 1.0) * band
        return max(0.0, raw)


@dataclass(frozen=True, slots=True)
class TimeoutPolicy:
    """Deadlines for a step.

    Attributes:
        per_attempt_s: each attempt is cancelled after this many seconds
            (``None`` = unbounded).
        total_s: the whole step (across retries + backoff) is abandoned after
            this many seconds (``None`` = unbounded).
    """

    per_attempt_s: float | None = None
    total_s: float | None = None

    def __post_init__(self) -> None:
        for value in (self.per_attempt_s, self.total_s):
            if value is not None and value <= 0:
                raise ValueError("timeout seconds must be > 0 or None")


def _seeded_unit(seed: str) -> float:
    """A deterministic float in ``[0, 1)`` from a string seed."""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    # Top 8 bytes → 64-bit int → unit interval.
    n = int.from_bytes(digest[:8], "big")
    return n / float(1 << 64)


#: A sensible default for cheap, idempotent steps.
DEFAULT_RETRY = RetryPolicy()
#: No retry — for steps whose failure should immediately compensate.
NO_RETRY = RetryPolicy(max_attempts=1)
#: No deadlines.
NO_TIMEOUT = TimeoutPolicy()


__all__ = [
    "DEFAULT_RETRY",
    "NO_RETRY",
    "NO_TIMEOUT",
    "RetryPolicy",
    "TimeoutPolicy",
]
