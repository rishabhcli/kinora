"""Async rate control + retry for the Phase-A page-analysis fan-out (§9.1 step 2).

§9.1 says "use the **batch API** for all of step 2's page analysis" precisely
because a large back-catalogue ingest can fire thousands of VL calls. Until the
batch lane lands, we run real concurrent calls (a semaphore in
:mod:`app.ingest.analyze`) — but raw concurrency on a hosted endpoint trips the
``429 Throttling.RateQuota`` the design warns about (AGENTS.md). Two primitives
fix that:

* :class:`TokenBucket` — an async, fair token bucket that smooths the *rate* of
  calls (requests/second with a burst) on top of the existing concurrency cap.
  It uses an **injectable monotonic clock + sleep** so the back-pressure timing
  is unit-testable with no real waiting.
* :func:`retrying` — wraps one provider coroutine with bounded exponential
  backoff + full jitter, retrying only on errors that *look* transient (a 429 /
  throttle / timeout / connection reset), so a momentary quota blip does not fail
  a page (and ultimately the whole book).

Both are pure async helpers — no DB, no network, no global state — so the
analyse step can opt in via settings and the tests stay fast + deterministic.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from app.core.logging import get_logger

logger = get_logger("app.ingest.ratelimit")

T = TypeVar("T")

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]

#: Substrings that mark a provider error as worth retrying (case-insensitive).
_TRANSIENT_MARKERS = (
    "429",
    "throttl",
    "ratequota",
    "rate limit",
    "rate_limit",
    "timeout",
    "timed out",
    "temporarily",
    "503",
    "502",
    "504",
    "connection reset",
    "connection aborted",
    "server error",
    "overloaded",
)


class TokenBucket:
    """A fair async token bucket: at most ``rate`` acquisitions/second with burst.

    ``acquire`` returns immediately while tokens remain, otherwise it sleeps just
    long enough for the next token to refill. Fairness comes from an ``asyncio``
    lock around the refill+deduct so concurrent waiters are serviced in order.

    A ``rate <= 0`` disables limiting (every acquire is a no-op) so the caller can
    leave the limiter wired but inert.
    """

    def __init__(
        self,
        rate_per_s: float,
        burst: int,
        *,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        self.rate = max(0.0, float(rate_per_s))
        self.capacity = max(1, int(burst))
        self._tokens = float(self.capacity)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._last = self._clock()
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.rate > 0.0

    def _refill(self) -> None:
        now = self._clock()
        elapsed = max(0.0, now - self._last)
        self._last = now
        self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available, then deduct them."""
        if not self.enabled:
            return
        want = min(max(tokens, 0.0), float(self.capacity))
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= want:
                    self._tokens -= want
                    return
                deficit = want - self._tokens
                wait = deficit / self.rate if self.rate > 0 else 0.0
            await self._sleep(max(wait, 0.0))


def is_transient(exc: BaseException) -> bool:
    """Whether an exception looks like a transient/retryable provider error."""
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


async def retrying(
    func: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 1.0,
    max_delay_s: float = 30.0,
    sleep: Sleeper | None = None,
    rng: random.Random | None = None,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Call ``func`` with bounded exponential-backoff-with-jitter retry.

    Retries only when :func:`is_transient` says the error is worth retrying;
    non-transient errors (and the final attempt) propagate immediately. The delay
    for attempt *n* (1-indexed) is ``random_uniform(0, base * 2**(n-1))`` capped at
    ``max_delay_s`` (the "full jitter" strategy), which avoids the thundering-herd
    a fixed backoff causes when many pages retry in lockstep.
    """
    sleeper = sleep or asyncio.sleep
    randomizer = rng or random
    attempts = max(1, max_attempts)
    last: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except Exception as exc:  # noqa: BLE001 - decide retry vs propagate below
            last = exc
            if attempt >= attempts or not is_transient(exc):
                raise
            ceiling = min(max_delay_s, base_delay_s * (2 ** (attempt - 1)))
            delay = randomizer.uniform(0.0, ceiling)
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            else:
                logger.warning(
                    "ingest.retry",
                    attempt=attempt,
                    max_attempts=attempts,
                    delay_s=round(delay, 3),
                    error=str(exc),
                )
            await sleeper(delay)
    # Unreachable (the loop either returns or raises), but satisfies the type.
    assert last is not None  # noqa: S101
    raise last


__all__ = [
    "Clock",
    "Sleeper",
    "TokenBucket",
    "is_transient",
    "retrying",
]
