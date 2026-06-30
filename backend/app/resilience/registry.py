"""Per-dependency registries — one breaker / bulkhead / limiter per dependency.

Isolation is the whole point of the breaker and the bulkhead: a flapping image
model must not trip the chat breaker or starve Redis's slots. That only works if
each *dependency* gets its own instance. These registries hold a family keyed by a
dependency name (``"dashscope.image"``, ``"redis.queue"``, ``"db.write"``), creating
each lazily on first use with a default config (or a per-name override).

Creation is guarded by a registry-level lock; each created instance then carries its
own lock. Snapshots fan out for a single ``/health``-style resilience view.

This mirrors the provider-layer ``BreakerRegistry`` but generalizes it to all three
isolation primitives and to any dependency, so a single shared
:class:`ResilienceRegistry` can back the whole process.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict

from .breaker import BreakerConfig, BreakerSnapshot, CircuitBreaker
from .bulkhead import Bulkhead, BulkheadConfig, BulkheadSnapshot
from .clock import SYSTEM_CLOCK, Clock
from .ratelimit import RateLimiter, TokenBucket, TokenBucketConfig


class BreakerRegistry:
    """Lazily-created :class:`CircuitBreaker` family keyed by dependency name."""

    def __init__(
        self,
        config: BreakerConfig | None = None,
        *,
        clock: Clock = SYSTEM_CLOCK,
        overrides: dict[str, BreakerConfig] | None = None,
    ) -> None:
        self._default = config or BreakerConfig()
        self._clock = clock
        self._overrides = dict(overrides or {})
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> CircuitBreaker:
        existing = self._breakers.get(name)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._breakers.get(name)
            if existing is None:
                cfg = self._overrides.get(name, self._default)
                existing = CircuitBreaker(name, cfg, clock=self._clock)
                self._breakers[name] = existing
            return existing

    def peek(self, name: str) -> CircuitBreaker | None:
        return self._breakers.get(name)

    def register(self, name: str, breaker: CircuitBreaker) -> None:
        """Insert a pre-built breaker (e.g. with a bespoke config) under ``name``."""
        self._breakers[name] = breaker

    def snapshots(self) -> list[BreakerSnapshot]:
        return [b.snapshot() for b in self._breakers.values()]

    def names(self) -> list[str]:
        return list(self._breakers.keys())


class BulkheadRegistry:
    """Lazily-created :class:`Bulkhead` family keyed by dependency name."""

    def __init__(
        self,
        config: BulkheadConfig | None = None,
        *,
        overrides: dict[str, BulkheadConfig] | None = None,
    ) -> None:
        self._default = config or BulkheadConfig()
        self._overrides = dict(overrides or {})
        self._bulkheads: dict[str, Bulkhead] = {}
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> Bulkhead:
        existing = self._bulkheads.get(name)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._bulkheads.get(name)
            if existing is None:
                cfg = self._overrides.get(name, self._default)
                existing = Bulkhead(name, cfg)
                self._bulkheads[name] = existing
            return existing

    def peek(self, name: str) -> Bulkhead | None:
        return self._bulkheads.get(name)

    def register(self, name: str, bulkhead: Bulkhead) -> None:
        self._bulkheads[name] = bulkhead

    def snapshots(self) -> list[BulkheadSnapshot]:
        return [b.snapshot() for b in self._bulkheads.values()]

    def names(self) -> list[str]:
        return list(self._bulkheads.keys())


class RateLimiterRegistry:
    """Family of :class:`RateLimiter` s keyed by dependency name.

    Unlike the others there is no universal default *shape* (token bucket vs sliding
    window is a per-dependency decision), so a limiter must be :meth:`register`-ed or
    created with an explicit factory; :meth:`get` falls back to a default token
    bucket only when no override exists.
    """

    def __init__(
        self,
        *,
        clock: Clock = SYSTEM_CLOCK,
        overrides: dict[str, RateLimiter] | None = None,
        default_config: TokenBucketConfig | None = None,
    ) -> None:
        self._clock = clock
        self._limiters: dict[str, RateLimiter] = dict(overrides or {})
        self._default_config = default_config or TokenBucketConfig()
        self._lock = asyncio.Lock()

    async def get(self, name: str) -> RateLimiter:
        existing = self._limiters.get(name)
        if existing is not None:
            return existing
        async with self._lock:
            existing = self._limiters.get(name)
            if existing is None:
                existing = TokenBucket(name, self._default_config, clock=self._clock)
                self._limiters[name] = existing
            return existing

    def peek(self, name: str) -> RateLimiter | None:
        return self._limiters.get(name)

    def register(self, name: str, limiter: RateLimiter) -> None:
        self._limiters[name] = limiter

    def names(self) -> list[str]:
        return list(self._limiters.keys())


class ResilienceRegistry:
    """The single process-wide home for all per-dependency resilience primitives.

    Bundles a :class:`BreakerRegistry`, :class:`BulkheadRegistry` and
    :class:`RateLimiterRegistry` behind one object so the composition root can build
    one and hand it to every adopting client. Each sub-registry is independent and
    lazily populated.
    """

    def __init__(
        self,
        *,
        breaker_config: BreakerConfig | None = None,
        bulkhead_config: BulkheadConfig | None = None,
        clock: Clock = SYSTEM_CLOCK,
        breaker_overrides: dict[str, BreakerConfig] | None = None,
        bulkhead_overrides: dict[str, BulkheadConfig] | None = None,
        rate_limiters: dict[str, RateLimiter] | None = None,
    ) -> None:
        self.clock = clock
        self.breakers = BreakerRegistry(
            breaker_config, clock=clock, overrides=breaker_overrides
        )
        self.bulkheads = BulkheadRegistry(bulkhead_config, overrides=bulkhead_overrides)
        self.rate_limiters = RateLimiterRegistry(clock=clock, overrides=rate_limiters)

    def health(self) -> dict[str, object]:
        """A single resilience snapshot for a ``/health``-style endpoint."""
        return {
            "breakers": [asdict(s) for s in self.breakers.snapshots()],
            "bulkheads": [asdict(s) for s in self.bulkheads.snapshots()],
            "rate_limiters": self.rate_limiters.names(),
        }


__all__ = [
    "BreakerRegistry",
    "BulkheadRegistry",
    "RateLimiterRegistry",
    "ResilienceRegistry",
]
