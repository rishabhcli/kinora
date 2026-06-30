"""``resilient_call`` — one entry point that composes every policy in the right order.

Adopting the framework should be a one-liner at a call site, not a five-layer
hand-assembly. :func:`resilient_call` takes a no-arg coroutine factory and a
:class:`ResiliencePolicy` and runs it through the full stack, in the order that
makes each layer see the right thing:

    retry( for each attempt:
        rate-limit acquire        # don't even attempt if we're over our own rate
        bulkhead slot             # bound concurrency to this dependency
        breaker.before_call       # reject instantly if the dependency is known-down
        chaos.before_call         # (tests/local only) maybe inject a fault/latency
        timeout( fn() )           # bound the single attempt
        breaker.record_*          # score the attempt for the breaker
    )

Why this order:

* **Rate limit outermost (inside retry).** A retry that immediately re-fires would
  defeat our own client-side limiter; acquiring per-attempt means backoff + limiter
  both apply. It precedes the bulkhead so we don't burn a scarce concurrency slot on
  a call the limiter would just shed.
* **Bulkhead before the breaker** so a probe in HALF_OPEN still respects concurrency
  isolation.
* **Breaker gate before the call, scoring after** — the classic placement; the
  breaker observes the *attempt's* success/failure, and a :class:`CircuitOpen`
  rejection is itself retryable (a later attempt may be admitted once half-open).
* **Timeout innermost** so the breaker counts a hung attempt as a failure.

The factory is re-invoked per attempt (it must be cheap and side-effect-free to
*call* — it returns a fresh awaitable each time). Everything is driven by an injected
clock, so a fully-loaded policy (retry + breaker cooldown + rate limit) runs instantly
and deterministically in tests.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

from .breaker import CircuitBreaker
from .bulkhead import Bulkhead
from .chaos import ChaosMonkey
from .clock import SYSTEM_CLOCK, Clock
from .ratelimit import RateLimiter
from .retry import RetryOn, RetryPolicy
from .timeout import call_with_timeout

T = TypeVar("T")


@dataclass(slots=True)
class ResiliencePolicy:
    """A declarative bundle of the policies to apply to one logical dependency call.

    Any field left ``None`` is simply skipped, so a caller dials in exactly the
    layers it needs:

    * ``retry`` — a :class:`RetryPolicy`; when ``None`` the call is attempted once.
    * ``timeout_s`` — per-attempt ceiling (``None`` = unbounded attempt).
    * ``breaker`` — a :class:`CircuitBreaker` (usually fetched from a registry).
    * ``bulkhead`` — a :class:`Bulkhead`.
    * ``rate_limiter`` — a :class:`RateLimiter`.
    * ``chaos`` — a :class:`ChaosMonkey` (no-op unless armed; local/tests only).

    ``name`` is used in errors/logs. The whole thing is reusable across calls.
    """

    name: str = "call"
    retry: RetryPolicy | None = None
    timeout_s: float | None = None
    breaker: CircuitBreaker | None = None
    bulkhead: Bulkhead | None = None
    rate_limiter: RateLimiter | None = None
    chaos: ChaosMonkey | None = None
    #: Convenience: build a default :class:`RetryPolicy` with this retry-on predicate
    #: when ``retry`` is None but you still want retries. Ignored if ``retry`` is set.
    default_retry_on: RetryOn | None = field(default=None)
    clock: Clock = SYSTEM_CLOCK

    def _effective_retry(self) -> RetryPolicy:
        if self.retry is not None:
            return self.retry
        return RetryPolicy(
            max_attempts=3,
            retry_on=self.default_retry_on,
            name=self.name,
            clock=self.clock,
        )


async def _one_attempt(
    fn: Callable[[], Awaitable[T]],
    policy: ResiliencePolicy,
) -> T:
    """A single guarded attempt: rate-limit → bulkhead → breaker → chaos → timeout.

    The breaker is scored here (success/failure of *this attempt*) so its window and
    consecutive counters track per-attempt outcomes, which is what the retry loop's
    backoff is reacting to as well.
    """
    if policy.rate_limiter is not None:
        await policy.rate_limiter.acquire()

    async def _guarded() -> T:
        if policy.breaker is not None:
            await policy.breaker.before_call()
        try:
            if policy.chaos is not None:
                await policy.chaos.before_call()
            result = await call_with_timeout(fn(), policy.timeout_s, name=policy.name)
        except BaseException:
            if policy.breaker is not None:
                await policy.breaker.record_failure()
            raise
        else:
            if policy.breaker is not None:
                await policy.breaker.record_success()
            return result

    if policy.bulkhead is not None:
        async with policy.bulkhead.slot():
            return await _guarded()
    return await _guarded()


async def resilient_call(
    fn: Callable[[], Awaitable[T]],
    policy: ResiliencePolicy | None = None,
    *,
    rng_seed: int | None = None,
) -> T:
    """Run ``fn`` (a no-arg coroutine factory) through the full resilience stack.

    ``fn`` is invoked once per attempt and must return a *fresh* awaitable each call.
    Pass ``rng_seed`` to make the backoff jitter reproducible in a test. With no
    ``policy`` it falls back to a plain 3-attempt retry on the default predicate.
    """
    pol = policy or ResiliencePolicy()
    retry = pol._effective_retry()
    return await retry.execute(lambda: _one_attempt(fn, pol), rng_seed=rng_seed)


def resilient(
    policy: ResiliencePolicy | None = None,
    *,
    rng_seed: int | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator form of :func:`resilient_call`::

        @resilient(ResiliencePolicy(name="dashscope.chat", retry=..., breaker=...))
        async def call_chat(prompt: str) -> str: ...
    """
    import functools

    def _decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def _wrapped(*args: object, **kwargs: object) -> T:
            return await resilient_call(
                lambda: fn(*args, **kwargs), policy, rng_seed=rng_seed
            )

        return _wrapped

    return _decorator


__all__ = ["ResiliencePolicy", "resilient", "resilient_call"]
