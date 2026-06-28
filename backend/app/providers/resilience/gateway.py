"""The resilient provider gateway — composes every resilience primitive (§12.1).

:class:`ResilientGateway` wraps a single attempt callable with the full stack, in
the order that matters:

```
cache(get_or_compute)                # §12.3 dedup — a hit/coalesce never spends
  └─ breaker(before_call)            # per-model circuit; reject fast when open
       └─ retry loop (backoff+jitter)# §12.1 exponential backoff with full jitter
            └─ adaptive rate limit   # AIMD bucket; backs off on 429
                 └─ [hedge]          # opt-in duplicate copies for tail latency
                      └─ attempt()   # the real provider call (base.ProviderClient)
```

It is *transport-agnostic*: callers hand it an async ``attempt`` (a thunk that
performs one real call and raises a typed
:class:`~app.providers.errors.ProviderError` on failure) plus a ``GatewayCall``
describing the request (model, op, idempotency, cache key parts). The gateway owns
none of the round-1 transport code — it composes around it, so it never edits the
round-1 providers.

Sacred invariants honoured:

* **Spend gate untouched.** A :class:`~app.providers.errors.LiveVideoDisabled` (and
  any non-retryable :class:`~app.providers.errors.ProviderBadRequest`) is propagated
  unchanged, is **never** counted as a breaker/rate-limit fault, and is **never**
  hedged or retried.
* **429s shape the rate, not just the retry.** A
  :class:`~app.providers.errors.RateLimited` feeds the adaptive bucket's
  multiplicative decrease *in addition* to driving a (Retry-After-respecting)
  backoff.
* **Determinism.** Clock, RNG, and sleep are all injectable, so the whole loop is
  reproducible in tests with zero real waiting.
"""

from __future__ import annotations

import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from app.core.logging import get_logger

from ..errors import (
    LiveVideoDisabled,
    ProviderError,
    RateLimited,
    TransientProviderError,
)
from .backoff import BackoffPolicy, BackoffSchedule
from .breakers import BreakerConfig, BreakerRegistry
from .cache import CacheConfig, ResponseCache, request_hash
from .hedging import HedgedExecutor, HedgePolicy
from .metering import MeteringSink
from .stats import GatewayCallStats, GatewaySnapshot

logger = get_logger("app.providers.resilience.gateway")

R = TypeVar("R")

#: Injectable async sleep (so the retry loop never waits for real time in tests).
AsyncSleep = Callable[[float], Awaitable[None]]
#: Injectable monotonic clock shared across breakers + rate limiter.
Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Top-level tunables; sub-configs default to their own sensible values."""

    max_attempts: int = 4
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    breaker: BreakerConfig = field(default_factory=BreakerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    hedge: HedgePolicy = field(default_factory=lambda: HedgePolicy(max_attempts=1))
    #: When False the gateway skips the cache layer entirely (no key computed).
    cache_enabled: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


@dataclass(frozen=True, slots=True)
class GatewayCall:
    """Describes one logical call so the gateway can route it correctly.

    Attributes:
        model: The model id (keys the per-model breaker + metering).
        op: Coarse operation label (``chat``/``image``/``video`` …).
        idempotent: Safe to hedge/duplicate. **Never set for video renders.**
        cacheable: Eligible for the response cache + in-flight dedup.
        cache_payload: The request parts that define cache identity (hashed). When
            ``None`` and ``cacheable``, the call is treated as uncacheable.
        retryable_override: Force the call non-retryable regardless of error type
            (e.g. a streaming call the caller doesn't want re-issued).
    """

    model: str
    op: str
    idempotent: bool = False
    cacheable: bool = False
    cache_payload: Any = None
    retryable_override: bool | None = None


class ResilientGateway:
    """Compose breakers + adaptive rate limit + retry/backoff + hedge + cache.

    One gateway is shared across a provider bundle (like the round-1 client), so the
    breakers/rate-limit/cache are process-coherent. The gateway does **not** create
    HTTP transports; callers pass an ``attempt`` thunk that uses the round-1
    :class:`~app.providers.base.ProviderClient`.
    """

    def __init__(
        self,
        config: GatewayConfig | None = None,
        *,
        metering: MeteringSink | None = None,
        rate_limiter: Any | None = None,
        clock: Clock = time.monotonic,
        rng: random.Random | None = None,
        sleep: AsyncSleep | None = None,
    ) -> None:
        from .ratelimit import AdaptiveRateConfig, AdaptiveTokenBucket

        self.config = config or GatewayConfig()
        self._clock = clock
        self._rng = rng or random.Random()
        import asyncio

        self._sleep: AsyncSleep = sleep or asyncio.sleep
        self.metering = metering or MeteringSink()
        # The rate limiter shares the gateway's injected clock *and* sleep so a test
        # with a fake clock advances it deterministically instead of waiting on the
        # wall clock when the adaptive rate forces a wait.
        self._rate = rate_limiter or AdaptiveTokenBucket(
            AdaptiveRateConfig(), clock=clock, sleep=self._sleep
        )
        self._breakers = BreakerRegistry(self.config.breaker, clock=clock)
        self._cache: ResponseCache[Any] = ResponseCache(self.config.cache, clock=clock)
        self._hedger = HedgedExecutor(self.config.hedge, sleep=self._sleep)
        self._stats = _MutableStats()

    # -- introspection ---------------------------------------------------- #

    @property
    def breakers(self) -> BreakerRegistry:
        return self._breakers

    @property
    def cache(self) -> ResponseCache[Any]:
        return self._cache

    @property
    def rate(self) -> float:
        return self._rate.rate

    def snapshot(self) -> GatewaySnapshot:
        return GatewaySnapshot(
            rate=self._rate.rate,
            in_cooldown=self._rate.in_cooldown(),
            calls=self._stats.frozen(),
            breakers=self._breakers.snapshots(),
            cache=self._cache.stats,
            hedging=self._hedger.stats,
            metering=self.metering.snapshot(),
        )

    # -- the entry point -------------------------------------------------- #

    async def execute(self, call: GatewayCall, attempt: Callable[[], Awaitable[R]]) -> R:
        """Run ``attempt`` for ``call`` under the full resilience stack.

        ``attempt`` performs exactly one real provider call and raises a typed
        :class:`~app.providers.errors.ProviderError` on failure. The gateway adds
        the cache, breaker, retry, rate-limit, and (opt-in) hedge layers around it.
        """
        self._stats.calls += 1
        if not (self.config.cache_enabled and call.cacheable and call.cache_payload is not None):
            return await self._guarded(call, attempt)

        key = request_hash(call.model, call.op, call.cache_payload)
        before_hits = self._cache.stats.hits

        async def compute() -> R:
            return await self._guarded(call, attempt)

        result = await self._cache.get_or_compute(key, compute)
        if self._cache.stats.hits > before_hits:
            self._stats.cache_hits += 1
        return result

    # -- breaker + retry + rate-limit + hedge ----------------------------- #

    async def _guarded(self, call: GatewayCall, attempt: Callable[[], Awaitable[R]]) -> R:
        breaker = await self._breakers.get(call.model)
        schedule = BackoffSchedule(self.config.backoff, rng=self._rng)
        last_error: ProviderError | None = None

        for attempt_no in range(1, self.config.max_attempts + 1):
            try:
                await breaker.before_call()
            except ProviderError:
                self._stats.breaker_rejections += 1
                self._stats.failures += 1
                self.metering.record_error(call.model, call.op)
                raise

            try:
                result = await self._one_attempt(call, attempt)
            except LiveVideoDisabled:
                # The spend gate is sacred: not a fault. Do NOT touch breaker/rate,
                # do NOT retry. Surface unchanged.
                raise
            except ProviderError as exc:
                await breaker.record_failure()
                last_error = exc
                if isinstance(exc, RateLimited):
                    self._rate.record_throttle()
                    self._stats.throttles_observed += 1
                retryable = self._is_retryable(call, exc)
                self.metering.record_error(call.model, call.op)
                logger.warning(
                    "gateway.attempt_failed",
                    op=call.op,
                    model=call.model,
                    attempt=attempt_no,
                    error=type(exc).__name__,
                    retryable=retryable,
                    status=exc.status_code,
                )
                if not retryable or attempt_no >= self.config.max_attempts:
                    self._stats.failures += 1
                    raise
                self._stats.retries += 1
                delay = schedule.next_delay(
                    attempt_no, retry_after_s=getattr(exc, "retry_after_s", None)
                )
                await self._sleep(delay)
                continue

            await breaker.record_success()
            self._rate.record_success()
            self._stats.successes += 1
            return result

        # Unreachable: the loop either returns or raises, but keep mypy + safety net.
        assert last_error is not None  # pragma: no cover
        raise last_error  # pragma: no cover

    async def _one_attempt(self, call: GatewayCall, attempt: Callable[[], Awaitable[R]]) -> R:
        """Run the attempt once, hedged if opted-in; gated by the rate limiter."""
        if call.idempotent and self.config.hedge.max_attempts > 1:

            async def hedged_copy(_idx: int) -> R:
                await self._rate.acquire()
                return await attempt()

            return await self._hedger.run(hedged_copy)
        await self._rate.acquire()
        return await attempt()

    def _is_retryable(self, call: GatewayCall, exc: ProviderError) -> bool:
        if call.retryable_override is not None:
            return call.retryable_override
        # A RateLimited / TransientProviderError is retryable; bad-request is not.
        return isinstance(exc, TransientProviderError) or exc.retryable

    async def aclose(self) -> None:
        await self._cache.clear()


@dataclass
class _MutableStats:
    """Internal mutable tally; frozen into :class:`GatewayCallStats` on snapshot."""

    calls: int = 0
    successes: int = 0
    failures: int = 0
    retries: int = 0
    breaker_rejections: int = 0
    throttles_observed: int = 0
    cache_hits: int = 0

    def frozen(self) -> GatewayCallStats:
        return GatewayCallStats(
            calls=self.calls,
            successes=self.successes,
            failures=self.failures,
            retries=self.retries,
            breaker_rejections=self.breaker_rejections,
            throttles_observed=self.throttles_observed,
            cache_hits=self.cache_hits,
        )


__all__ = [
    "AsyncSleep",
    "Clock",
    "GatewayCall",
    "GatewayConfig",
    "ResilientGateway",
]
