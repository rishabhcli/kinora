"""Pure exponential-backoff-with-jitter helpers + retry classification.

Kept dependency-free and side-effect-free (the actual sleeping is the caller's
job, through the injected :class:`~app.integrations.clock.Clock`) so the schedule
is a value you can assert on. The jitter source is injectable for the same
reason — tests pass a deterministic one.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from app.integrations.errors import (
    AuthExpired,
    ConfigurationError,
    PermanentError,
    RateLimited,
    TransientError,
)


@dataclass(frozen=True)
class BackoffPolicy:
    """Exponential backoff with full jitter, capped.

    delay(attempt) = min(cap, base * factor**attempt), then jittered in
    ``[delay*(1-jitter), delay*(1+jitter)]``. A :class:`RateLimited` error's
    ``retry_after_s`` always wins over the computed delay (honour the server).
    """

    base_s: float = 0.5
    factor: float = 2.0
    cap_s: float = 60.0
    jitter: float = 0.25
    max_attempts: int = 5

    def base_delay(self, attempt: int) -> float:
        """The un-jittered delay before retry ``attempt`` (0-indexed)."""
        if attempt < 0:
            attempt = 0
        return min(self.cap_s, self.base_s * (self.factor**attempt))

    def delay(
        self,
        attempt: int,
        *,
        retry_after_s: float | None = None,
        rand: Callable[[], float] | None = None,
    ) -> float:
        """The actual delay before retry ``attempt``, jittered and capped.

        Args:
            attempt: the 0-indexed retry number.
            retry_after_s: a server ``Retry-After`` hint that overrides the
                computed delay when present.
            rand: a ``() -> float in [0,1)`` jitter source (defaults to
                :func:`random.random`); injected in tests for determinism.
        """
        if retry_after_s is not None:
            return min(self.cap_s, max(0.0, retry_after_s))
        d = self.base_delay(attempt)
        if self.jitter <= 0:
            return d
        r = (rand or random.random)()
        low = d * (1.0 - self.jitter)
        high = d * (1.0 + self.jitter)
        return min(self.cap_s, low + (high - low) * r)


def is_retryable(exc: BaseException) -> bool:
    """Whether ``exc`` is worth retrying.

    Retryable: :class:`TransientError` (and its :class:`RateLimited` subclass).
    Not retryable: :class:`PermanentError`, :class:`AuthExpired`,
    :class:`ConfigurationError`, and anything outside the hierarchy (a bug — let
    it surface).
    """
    if isinstance(exc, (PermanentError, AuthExpired, ConfigurationError)):
        return False
    return isinstance(exc, (TransientError, RateLimited))


def retry_after_of(exc: BaseException) -> float | None:
    """Extract a :class:`RateLimited` server hint, if any."""
    if isinstance(exc, RateLimited):
        return exc.retry_after_s
    return None


__all__ = ["BackoffPolicy", "is_retryable", "retry_after_of"]
