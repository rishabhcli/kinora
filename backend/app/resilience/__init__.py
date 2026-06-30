"""A unified resilience framework for every external call in Kinora.

Provider calls (video/image/tts/llm), Redis queue ops and Postgres writes all face
the same failure modes — transient blips, throttling, hung connections, cascading
outages — and today each subsystem reinvents its own ad-hoc retry. This package is
the *one* composable toolkit they can all adopt:

* [retry][app.resilience.retry] — an async :class:`RetryPolicy` (decorator **and**
  context manager): max attempts, exponential backoff with full jitter, a retry-on
  predicate, a wall-clock deadline budget, and an on-retry hook. Time is injected so
  retry tests are instant.
* [backoff][app.resilience.backoff] — full / equal / decorrelated jitter schedules,
  Retry-After-aware, reproducible under a seeded RNG.
* [breaker][app.resilience.breaker] — a closed/open/half-open
  :class:`CircuitBreaker` that trips on *either* consecutive failures *or* a rolling
  failure rate, with a cooldown and a half-open probe budget.
* [bulkhead][app.resilience.bulkhead] — a semaphore-based concurrency limiter that
  isolates each dependency and sheds load (:class:`BulkheadFull`) under overload.
* [timeout][app.resilience.timeout] — a per-attempt timeout wrapper raising the
  taxonomy's :class:`CallTimeout`.
* [ratelimit][app.resilience.ratelimit] — client-side :class:`TokenBucket` and
  :class:`SlidingWindowLimiter`.
* [chaos][app.resilience.chaos] — a probabilistic fault/latency injector, **off in
  prod** (refuses to arm outside ``local``), for proving the policies in tests.
* [registry][app.resilience.registry] — per-dependency families of the above.
* [composite][app.resilience.composite] — :func:`resilient_call` /
  :class:`ResiliencePolicy`, the single front door that composes every layer in the
  correct order, plus the :func:`resilient` decorator.
* [errors][app.resilience.errors] — the typed taxonomy
  (:class:`TransientError` / :class:`PermanentError` / decision wrappers) +
  :func:`classify_exception`.
* [clock][app.resilience.clock] — the injected :class:`Clock` (real
  :data:`SYSTEM_CLOCK`, deterministic :class:`ManualClock`).

Nothing here calls a provider, touches Redis/Postgres, reads settings, or sleeps on
import; the framework is pure policy + an injected clock, opt-in per call site.
"""

from __future__ import annotations

from .backoff import BackoffPolicy, BackoffSchedule, JitterStrategy
from .breaker import BreakerConfig, BreakerSnapshot, BreakerState, CircuitBreaker
from .bulkhead import Bulkhead, BulkheadConfig, BulkheadSnapshot
from .chaos import ChaosConfig, ChaosFault, ChaosMonkey, chaos_from_settings
from .clock import SYSTEM_CLOCK, AsyncSleep, Clock, ManualClock, MonotonicFn, SystemClock
from .composite import ResiliencePolicy, resilient, resilient_call
from .errors import (
    AuthError,
    BulkheadFull,
    CallTimeout,
    ChaosInjectedError,
    CircuitOpen,
    DeadlineExceeded,
    PermanentError,
    RateLimitedError,
    RateLimitExceeded,
    ResilienceError,
    RetriesExhausted,
    TimeoutError_,
    TransientError,
    classify_exception,
)
from .ratelimit import (
    RateLimiter,
    SlidingWindowConfig,
    SlidingWindowLimiter,
    TokenBucket,
    TokenBucketConfig,
)
from .registry import (
    BreakerRegistry,
    BulkheadRegistry,
    RateLimiterRegistry,
    ResilienceRegistry,
)
from .retry import (
    OnRetry,
    RetryAttempt,
    RetryContext,
    RetryOn,
    RetryPolicy,
    RetryPredicate,
    retryable,
)
from .timeout import call_with_timeout, timeout

__all__ = [
    "SYSTEM_CLOCK",
    "AsyncSleep",
    "AuthError",
    "BackoffPolicy",
    "BackoffSchedule",
    "BreakerConfig",
    "BreakerRegistry",
    "BreakerSnapshot",
    "BreakerState",
    "Bulkhead",
    "BulkheadConfig",
    "BulkheadFull",
    "BulkheadRegistry",
    "BulkheadSnapshot",
    "CallTimeout",
    "ChaosConfig",
    "ChaosFault",
    "ChaosInjectedError",
    "ChaosMonkey",
    "CircuitBreaker",
    "CircuitOpen",
    "Clock",
    "DeadlineExceeded",
    "JitterStrategy",
    "ManualClock",
    "MonotonicFn",
    "OnRetry",
    "PermanentError",
    "RateLimitExceeded",
    "RateLimitedError",
    "RateLimiter",
    "RateLimiterRegistry",
    "ResilienceError",
    "ResiliencePolicy",
    "ResilienceRegistry",
    "RetriesExhausted",
    "RetryAttempt",
    "RetryContext",
    "RetryOn",
    "RetryPolicy",
    "RetryPredicate",
    "SlidingWindowConfig",
    "SlidingWindowLimiter",
    "SystemClock",
    "TimeoutError_",
    "TokenBucket",
    "TokenBucketConfig",
    "TransientError",
    "chaos_from_settings",
    "classify_exception",
    "call_with_timeout",
    "resilient",
    "resilient_call",
    "retryable",
    "timeout",
]
