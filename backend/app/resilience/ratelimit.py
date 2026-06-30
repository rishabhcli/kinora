"""Client-side rate limiting — token bucket *and* sliding window.

These are *our* limiters protecting a downstream from *us* (distinct from a server
429, which is the downstream protecting itself — that's
:class:`~app.resilience.errors.RateLimitedError`). Two shapes, one
:class:`RateLimiter` protocol so :func:`~app.resilience.composite.resilient_call`
can take either:

* :class:`TokenBucket` — continuous refill at ``rate`` tokens/s with a ``burst``
  capacity. Smooths a bursty caller to an average rate while still allowing short
  bursts. The classic choice for "N requests per second, but a little spiky is ok."
* :class:`SlidingWindowLimiter` — at most ``max_events`` in any rolling
  ``window_s``. A hard, precise cap with no burst leniency (great for a strict
  "≤ 60 calls/minute" quota where the provider counts a true window).

Both expose ``acquire`` (blocks until permitted, using the injected clock's sleep —
so a test advances virtual time instead of waiting) and ``try_acquire`` (non-blocking;
returns False or, via :meth:`acquire(..., block=False)`, raises
:class:`RateLimitExceeded`). Time is injected via a
:class:`~app.resilience.clock.Clock`, so behaviour is exhaustively testable without
real sleeping.
"""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .clock import SYSTEM_CLOCK, Clock
from .errors import RateLimitExceeded

#: Tolerance absorbing floating-point dust so a refill landing a hair below the
#: request (0.999…) still counts as available and never traps the acquire loop.
_EPSILON = 1e-9


@runtime_checkable
class RateLimiter(Protocol):
    """The minimal limiter surface the composite call depends on."""

    name: str

    async def acquire(self, tokens: float = 1.0, *, block: bool = True) -> bool:
        """Acquire ``tokens`` of permit. Returns True on success.

        When ``block`` is True, waits (via the injected clock) until permitted and
        always returns True. When False, returns immediately — True if permitted now,
        else raises :class:`RateLimitExceeded`.
        """
        ...

    def try_acquire(self, tokens: float = 1.0) -> bool:
        """Non-raising, non-blocking probe: permitted right now?"""
        ...


@dataclass(frozen=True, slots=True)
class TokenBucketConfig:
    rate: float = 8.0  # tokens per second (steady-state)
    burst: float = 8.0  # bucket capacity (instantaneous burst)

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("rate must be > 0")
        if self.burst <= 0:
            raise ValueError("burst must be > 0")


class TokenBucket:
    """A continuous-refill token bucket (see module docstring)."""

    def __init__(
        self,
        name: str = "ratelimit",
        config: TokenBucketConfig | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.name = name
        self.config = config or TokenBucketConfig()
        self._clock = clock
        self._capacity = float(self.config.burst)
        self._tokens = self._capacity
        self._updated = clock.monotonic()
        self._lock = asyncio.Lock()

    @property
    def available_tokens(self) -> float:
        return self._tokens

    def _refill_locked(self) -> None:
        now = self._clock.monotonic()
        elapsed = max(now - self._updated, 0.0)
        self._tokens = min(self._capacity, self._tokens + elapsed * self.config.rate)
        self._updated = now

    def try_acquire(self, tokens: float = 1.0) -> bool:
        # Synchronous probe: refill + take if available. Not lock-guarded (a probe
        # is advisory); acquire() does the authoritative, locked take.
        self._refill_locked()
        if self._tokens + _EPSILON >= tokens:
            self._tokens = max(0.0, self._tokens - tokens)
            return True
        return False

    async def acquire(self, tokens: float = 1.0, *, block: bool = True) -> bool:
        if tokens > self._capacity:
            raise ValueError(
                f"requested {tokens} tokens exceeds bucket capacity {self._capacity}"
            )
        while True:
            async with self._lock:
                self._refill_locked()
                if self._tokens + _EPSILON >= tokens:
                    self._tokens = max(0.0, self._tokens - tokens)
                    return True
                if not block:
                    raise RateLimitExceeded(
                        f"rate limit {self.name!r} exhausted "
                        f"({self._tokens:.3f}/{tokens} tokens)",
                        name=self.name,
                    )
                deficit = tokens - self._tokens
                wait_s = deficit / self.config.rate
            await self._clock.sleep(wait_s)


@dataclass(frozen=True, slots=True)
class SlidingWindowConfig:
    max_events: int = 60
    window_s: float = 60.0

    def __post_init__(self) -> None:
        if self.max_events < 1:
            raise ValueError("max_events must be >= 1")
        if self.window_s <= 0:
            raise ValueError("window_s must be > 0")


class SlidingWindowLimiter:
    """At most ``max_events`` permits in any rolling ``window_s`` (see docstring)."""

    def __init__(
        self,
        name: str = "ratelimit",
        config: SlidingWindowConfig | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self.name = name
        self.config = config or SlidingWindowConfig()
        self._clock = clock
        self._events: deque[float] = deque()
        self._lock = asyncio.Lock()

    def _evict_locked(self, now: float) -> None:
        cutoff = now - self.config.window_s
        while self._events and self._events[0] <= cutoff:
            self._events.popleft()

    @property
    def current_count(self) -> int:
        self._evict_locked(self._clock.monotonic())
        return len(self._events)

    def try_acquire(self, tokens: float = 1.0) -> bool:
        # Sliding window counts whole events; ``tokens`` is treated as a count.
        count = max(1, int(tokens))
        now = self._clock.monotonic()
        self._evict_locked(now)
        if len(self._events) + count <= self.config.max_events:
            self._events.extend([now] * count)
            return True
        return False

    async def acquire(self, tokens: float = 1.0, *, block: bool = True) -> bool:
        count = max(1, int(tokens))
        if count > self.config.max_events:
            raise ValueError(
                f"requested {count} events exceeds window cap {self.config.max_events}"
            )
        while True:
            async with self._lock:
                now = self._clock.monotonic()
                self._evict_locked(now)
                if len(self._events) + count <= self.config.max_events:
                    self._events.extend([now] * count)
                    return True
                if not block:
                    raise RateLimitExceeded(
                        f"rate limit {self.name!r} window full "
                        f"({len(self._events)}/{self.config.max_events} in "
                        f"{self.config.window_s}s)",
                        name=self.name,
                    )
                # Wait until the oldest event ages out enough to fit ``count``.
                # The (count)-th oldest event is the one that must expire.
                idx = count - 1
                oldest = self._events[idx] if idx < len(self._events) else self._events[0]
                wait_s = max(oldest + self.config.window_s - now, 0.0)
                # Guard against a zero/negative wait spinning the loop: nudge by a
                # fair share of the window so progress is always made.
                if wait_s <= 0:
                    wait_s = self.config.window_s / self.config.max_events
            await self._clock.sleep(wait_s)


__all__ = [
    "RateLimiter",
    "SlidingWindowConfig",
    "SlidingWindowLimiter",
    "TokenBucket",
    "TokenBucketConfig",
]
