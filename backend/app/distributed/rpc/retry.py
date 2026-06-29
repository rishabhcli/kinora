"""Retries with a *budget* — bounded re-issue under exponential backoff.

Naive retries amplify load: when a dependency is struggling, every caller piling
on N retries multiplies the offered load by N exactly when the system can least
afford it (a "retry storm"). The two guards here are the ones a real mesh uses:

* **Per-call attempt cap + exponential backoff with jitter.** A transient error
  is retried up to ``max_attempts`` with delays ``base * factor**n`` clamped to
  ``max_delay``, plus *decorrelated jitter* so a thundering herd of retries
  spreads out instead of re-synchronising. Backoff is computed against the
  injected :class:`Clock`; the actual wait is delegated to a ``sleep`` callable so
  tests advance a :class:`ManualClock` instead of sleeping.

* **A shared retry budget** (:class:`RetryBudget`): a token bucket of *extra*
  attempts allowed per unit of real traffic (e.g. "retries may add at most 20% on
  top of primary requests"). Once the budget is empty, retries are *suppressed*
  even though a single call would otherwise retry — so a system-wide outage can
  never be amplified into a self-inflicted DDoS. This is the gRPC / Envoy retry
  budget, modelled deterministically.

Only **retryable** statuses on **idempotent** methods are retried; a deadline
that has already expired short-circuits (no point burning an attempt).
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.distributed.rpc.deadline import Clock, Deadline
from app.distributed.rpc.errors import RpcError, RpcStatus

#: An async sleep seam: ``await sleep(seconds)``. Production wires ``anyio.sleep``;
#: tests wire one that advances a :class:`ManualClock` without real waiting.
SleepFn = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BackoffPolicy:
    """Exponential backoff with decorrelated jitter (pure delay math).

    ``delay(attempt)`` is ``min(max_delay, base * factor**attempt)`` jittered. The
    jitter mode follows AWS's "decorrelated jitter" recommendation by default: the
    next delay is drawn uniformly from ``[base, prev*3]`` clamped — which both
    spreads a herd *and* keeps growing, avoiding the "full jitter collapses to
    tiny waits" failure of plain randomization.
    """

    base_delay_s: float = 0.05
    factor: float = 2.0
    max_delay_s: float = 5.0
    jitter: str = "decorrelated"  # "decorrelated" | "full" | "none"

    def delay(self, attempt: int, *, rng: random.Random, prev_delay_s: float = 0.0) -> float:
        """The wait before retry ``attempt`` (0-based: attempt 0 = first retry)."""
        exp = min(self.max_delay_s, self.base_delay_s * (self.factor**attempt))
        if self.jitter == "none":
            return exp
        if self.jitter == "full":
            return rng.uniform(0.0, exp)
        # decorrelated: grow off the previous delay, clamped to max.
        prev = prev_delay_s if prev_delay_s > 0 else self.base_delay_s
        return min(self.max_delay_s, rng.uniform(self.base_delay_s, prev * 3.0))


@dataclass
class RetryBudget:
    """A token-bucket cap on *retries as a fraction of primary traffic*.

    Each primary request deposits ``ratio`` tokens (capped at ``max_tokens``);
    each retry withdraws one. When the bucket is empty, retries are denied even
    for an otherwise-retryable error. ``min_per_window`` floors the allowance so a
    low-traffic service can still retry a little. Deterministic — no time math, so
    it's trivially testable and thread-safe under a single-threaded event loop.
    """

    ratio: float = 0.2
    max_tokens: float = 100.0
    min_retries_floor: int = 3
    _tokens: float = field(default=0.0, init=False)
    _primary_seen: int = field(default=0, init=False)

    def record_primary(self) -> None:
        """Account a primary (first-attempt) request; deposits ``ratio`` tokens."""
        self._primary_seen += 1
        self._tokens = min(self.max_tokens, self._tokens + self.ratio)

    def try_withdraw(self) -> bool:
        """Attempt to spend one retry token. Returns whether a retry is allowed.

        The ``min_retries_floor`` lets the first few retries through regardless of
        accrued tokens, so a freshly-started or low-QPS caller is not starved.
        """
        if self._primary_seen <= self.min_retries_floor:
            return True
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        return False

    @property
    def tokens(self) -> float:
        """Current token balance (introspection / metrics)."""
        return self._tokens


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Declarative retry rules + the budget the policy shares across calls.

    A method is retried only when *all* hold: it is ``idempotent``, the error
    status is :meth:`RpcStatus.retryable`, attempts remain, the deadline has not
    expired, and the shared :class:`RetryBudget` grants a token. Non-retryable
    statuses (``INVALID_ARGUMENT``, ``NOT_FOUND``, …) fail fast — retrying them is
    pure waste.
    """

    max_attempts: int = 3
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    budget: RetryBudget = field(default_factory=RetryBudget)
    retry_non_idempotent: bool = False
    retryable_statuses: frozenset[RpcStatus] | None = None

    def is_retryable(self, status: RpcStatus, *, idempotent: bool) -> bool:
        """Whether an error with ``status`` may be retried for this method."""
        if not idempotent and not self.retry_non_idempotent:
            return False
        if self.retryable_statuses is not None:
            return status in self.retryable_statuses
        return status.retryable


async def run_with_retry(
    attempt_fn: Callable[[int], Awaitable[object]],
    *,
    policy: RetryPolicy,
    idempotent: bool,
    deadline: Deadline,
    clock: Clock,
    sleep: SleepFn,
    rng: random.Random | None = None,
) -> object:
    """Execute ``attempt_fn(attempt)`` with retry/backoff under the policy.

    ``attempt_fn`` is called with a 0-based attempt index and either returns the
    successful result or raises :class:`RpcError`. The loop:

    1. records one primary request against the budget;
    2. on a retryable error with attempts/deadline/budget remaining, sleeps the
       backoff delay (clamped to the remaining deadline) and re-issues;
    3. otherwise re-raises the last error (annotated with the attempt count).

    Returns the first successful result. The injected ``sleep`` + ``clock`` make
    every wait deterministic in tests.
    """
    rng = rng or random.Random()
    policy.budget.record_primary()
    last_error: RpcError | None = None
    prev_delay = 0.0
    for attempt in range(policy.max_attempts):
        if deadline.expired(clock=clock):
            if last_error is not None:
                raise last_error
            from app.distributed.rpc.errors import deadline_exceeded

            raise deadline_exceeded("deadline expired before first attempt")
        try:
            return await attempt_fn(attempt)
        except RpcError as err:
            last_error = err
            is_last = attempt == policy.max_attempts - 1
            if is_last or not policy.is_retryable(err.status, idempotent=idempotent):
                raise
            if not policy.budget.try_withdraw():
                # Budget exhausted: stop amplifying, surface the error now.
                raise
            delay = policy.backoff.delay(attempt, rng=rng, prev_delay_s=prev_delay)
            prev_delay = delay
            remaining = deadline.remaining(clock=clock)
            if remaining <= 0.0:
                raise
            await sleep(min(delay, remaining))
    # Unreachable in practice (the loop raises on the last attempt), but keeps the
    # type checker happy and is a defensive backstop.
    assert last_error is not None  # noqa: S101
    raise last_error


__all__ = [
    "BackoffPolicy",
    "RetryBudget",
    "RetryPolicy",
    "SleepFn",
    "run_with_retry",
]
