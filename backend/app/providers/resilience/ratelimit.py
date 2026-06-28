"""Adaptive token-bucket rate limiting (AIMD backoff on 429s).

The round-1 :class:`~app.providers.base.TokenBucket` is a *static* limiter: a
fixed refill rate, no feedback. The DashScope-intl free tier throttles the
**image** model independently (``429 Throttling.RateQuota``, CLAUDE.md), so a
static rate either runs too slow (wasting headroom) or too fast (eating 429s).

:class:`AdaptiveTokenBucket` adds **AIMD** congestion control — the same idea TCP
uses — on top of the bucket:

* **Additive increase.** Every success nudges the effective rate up by a small
  step toward ``max_rate`` (probe for more headroom).
* **Multiplicative decrease.** A throttle signal (a 429 / ``RateLimited``) halves
  the effective rate (``decrease_factor``), then enters a short *cooldown* during
  which the rate is not increased — so we don't immediately claw back into the
  wall we just hit.

It keeps the bucket semantics (continuous refill, burst capacity) of the round-1
limiter, so it is a drop-in for any code that calls ``await bucket.acquire()``.
Time is injected (a monotonic ``clock``) so behavior is exhaustively testable
without sleeping.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

#: Injectable monotonic clock (seconds). Tests pass a controllable fake.
Clock = Callable[[], float]
#: Injectable async sleep (so tests advance a fake clock instead of waiting).
AsyncSleep = Callable[[float], Awaitable[None]]

#: Tolerance for the "tokens available" check, to absorb floating-point dust so a
#: refill that lands a hair below the request never traps :meth:`acquire`.
_TOKEN_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class AdaptiveRateConfig:
    """Tunables for :class:`AdaptiveTokenBucket` (AIMD congestion control)."""

    #: Starting / steady-state target rate (tokens per second).
    initial_rate: float = 8.0
    #: Hard ceiling the additive-increase probe never exceeds.
    max_rate: float = 16.0
    #: Hard floor the multiplicative-decrease never drops below (keeps progress).
    min_rate: float = 0.5
    #: Burst capacity (tokens available instantly).
    burst: int = 8
    #: Additive increase per success, in tokens/s.
    increase_step: float = 0.25
    #: Multiplicative decrease factor applied on a throttle signal (0<f<1).
    decrease_factor: float = 0.5
    #: After a decrease, suppress increases for this long (let the wall settle).
    cooldown_s: float = 5.0

    def __post_init__(self) -> None:
        if not (0.0 < self.decrease_factor < 1.0):
            raise ValueError("decrease_factor must be in (0, 1)")
        if self.min_rate <= 0:
            raise ValueError("min_rate must be > 0")
        if self.max_rate < self.initial_rate or self.initial_rate < self.min_rate:
            raise ValueError("require min_rate <= initial_rate <= max_rate")


class AdaptiveTokenBucket:
    """An async token bucket whose refill rate adapts to throttle feedback.

    Usage mirrors the round-1 ``TokenBucket``::

        await bucket.acquire()          # before a call
        bucket.record_success()         # on 2xx
        bucket.record_throttle()        # on 429 / RateLimited

    The bucket refills continuously at the *current adaptive rate*; ``acquire``
    blocks until a token is available. All rate mutation is guarded by the same
    lock as the bucket math so the refill always sees a consistent rate.
    """

    def __init__(
        self,
        config: AdaptiveRateConfig | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: AsyncSleep | None = None,
    ) -> None:
        self.config = config or AdaptiveRateConfig()
        self._clock = clock
        # Injectable sleep so tests can advance a fake clock instead of real waits.
        self._sleep: AsyncSleep = sleep or asyncio.sleep
        self._rate = self.config.initial_rate
        self._capacity = float(max(self.config.burst, 1))
        self._tokens = self._capacity
        self._updated = clock()
        self._cooldown_until = 0.0
        self._throttle_events = 0
        self._success_events = 0
        self._lock = asyncio.Lock()

    # -- introspection ---------------------------------------------------- #

    @property
    def rate(self) -> float:
        """The current effective refill rate (tokens/s)."""
        return self._rate

    @property
    def throttle_events(self) -> int:
        return self._throttle_events

    @property
    def success_events(self) -> int:
        return self._success_events

    @property
    def available_tokens(self) -> float:
        """The (non-refilled) token count — for tests/inspection, not a guarantee."""
        return self._tokens

    # -- bucket math ------------------------------------------------------ #

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = max(now - self._updated, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Block until ``tokens`` are available at the current adaptive rate.

        A small epsilon tolerance on the availability check absorbs floating-point
        dust: without it, a refill that lands a hair below ``tokens`` (e.g. 0.999…)
        computes a sub-epsilon ``wait_s`` that, once added to a large clock value,
        rounds away to *zero* elapsed time — trapping the loop forever. The epsilon
        means "close enough" counts as available, so the loop always terminates.
        """
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens + _TOKEN_EPSILON >= tokens:
                    self._tokens = max(0.0, self._tokens - tokens)
                    return
                deficit = tokens - self._tokens
                wait_s = deficit / self._rate
            await self._sleep(wait_s)

    # -- AIMD feedback ---------------------------------------------------- #

    def record_success(self) -> None:
        """Additive-increase the rate toward ``max_rate`` (outside cooldown)."""
        now = self._clock()
        # Lock-free counters are fine; the rate read/write is a single float store
        # and acquire() re-reads it under the lock on the next refill.
        self._success_events += 1
        if now < self._cooldown_until:
            return
        self._rate = min(self.config.max_rate, self._rate + self.config.increase_step)

    def record_throttle(self) -> None:
        """Multiplicative-decrease the rate and enter a cooldown (a 429 was seen)."""
        self._throttle_events += 1
        self._rate = max(self.config.min_rate, self._rate * self.config.decrease_factor)
        self._cooldown_until = self._clock() + self.config.cooldown_s

    def in_cooldown(self) -> bool:
        """True while increases are suppressed after the last throttle."""
        return self._clock() < self._cooldown_until


__all__ = [
    "AdaptiveRateConfig",
    "AdaptiveTokenBucket",
    "AsyncSleep",
    "Clock",
]
