"""A composable async retry policy — decorator *and* context manager.

This is the workhorse: every external call that should survive a transient blip
runs inside a :class:`RetryPolicy`. It is deliberately small and explicit so the
behaviour is obvious from the call site:

* **max attempts** — total tries (not "retries"); ``max_attempts=3`` = 1 try + 2
  retries.
* **exponential backoff with full jitter** — delegated to
  [app.resilience.backoff][]; seeded RNG ⇒ reproducible.
* **retry-on predicate** — by default :func:`~app.resilience.errors.classify_exception`
  (retry transient/timeout/throttle/connection errors, fail fast on the rest); pass
  ``retry_on=`` an exception class / tuple / predicate to override.
* **deadline budget** — an optional wall-clock ceiling across *all* attempts; if the
  next backoff would blow it, we stop early with :class:`DeadlineExceeded` rather
  than oversleeping.
* **on-retry hook** — called with an :class:`RetryAttempt` before each sleep (for
  structured logging / metrics); never affects control flow.

Two front doors, one engine:

* As an **async context manager**, ``async with policy.attempt() as attempt:`` wraps
  a *block* — re-enter the ``async for`` to retry. The decorator and ``execute`` use
  this same engine, so they share semantics exactly.
* As a **decorator**, ``@policy.retry`` (or ``@retryable(...)``) wraps a coroutine.
* As a **one-shot**, ``await policy.execute(coro_factory)``.

Time is injected via a :class:`~app.resilience.clock.Clock`; tests pass a
:class:`~app.resilience.clock.ManualClock` whose ``sleep`` advances virtual time, so
a 5-attempt loop with 30 s caps finishes instantly.
"""

from __future__ import annotations

import functools
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from app.core.logging import get_logger

from .backoff import BackoffPolicy, BackoffSchedule
from .clock import SYSTEM_CLOCK, Clock
from .errors import (
    DeadlineExceeded,
    RateLimitedError,
    ResilienceError,
    RetriesExhausted,
    classify_exception,
)

logger = get_logger("app.resilience.retry")

T = TypeVar("T")

#: A predicate ``(exc) -> bool``: should this exception trigger a retry?
RetryPredicate = Callable[[BaseException], bool]
#: Accepted ``retry_on`` shapes: a predicate, one exception class, or a tuple.
RetryOn = RetryPredicate | type[BaseException] | tuple[type[BaseException], ...]


@dataclass(frozen=True, slots=True)
class RetryAttempt:
    """What the on-retry hook is handed before each backoff sleep.

    ``number`` is the 1-based attempt that just failed; ``delay_s`` is the sleep
    about to happen; ``elapsed_s`` is wall time since the loop started.
    """

    number: int
    exception: BaseException
    delay_s: float
    elapsed_s: float


OnRetry = Callable[[RetryAttempt], None]


def _as_predicate(retry_on: RetryOn | None) -> RetryPredicate:
    """Normalize the three ``retry_on`` shapes into a single predicate."""
    if retry_on is None:
        return classify_exception
    if isinstance(retry_on, type):
        klass: type[BaseException] = retry_on
        return lambda exc: isinstance(exc, klass)
    if isinstance(retry_on, tuple):
        classes = retry_on
        return lambda exc: isinstance(exc, classes)
    # Remaining shape per the RetryOn union: a predicate callable.
    predicate: RetryPredicate = retry_on
    return predicate


def _retry_after(exc: BaseException) -> float | None:
    """Pull a server ``Retry-After`` hint off an exception if it carries one."""
    if isinstance(exc, RateLimitedError):
        return exc.retry_after_s
    hint = getattr(exc, "retry_after_s", None)
    return float(hint) if isinstance(hint, int | float) else None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """An immutable, reusable retry configuration.

    Construct once (often module-level) and reuse: it holds no per-call state. The
    per-loop mutable state (the decorrelated-jitter walk, attempt counter) lives in
    a fresh :class:`BackoffSchedule` / loop created on each call.
    """

    max_attempts: int = 3
    backoff: BackoffPolicy = BackoffPolicy()
    retry_on: RetryOn | None = None
    #: Optional wall-clock budget (seconds) across all attempts; ``None`` = no cap.
    deadline_s: float | None = None
    on_retry: OnRetry | None = None
    name: str = "retry"
    clock: Clock = SYSTEM_CLOCK

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.deadline_s is not None and self.deadline_s <= 0:
            raise ValueError("deadline_s must be > 0 when set")

    @property
    def predicate(self) -> RetryPredicate:
        return _as_predicate(self.retry_on)

    # -- one-shot execution ---------------------------------------------- #

    async def execute(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        rng_seed: int | None = None,
    ) -> T:
        """Run ``fn`` (a no-arg coroutine factory) under this policy, returning its
        result or raising the terminal error.

        Raises the original (non-retryable) exception immediately if the predicate
        rejects it; :class:`RetriesExhausted` when attempts run out;
        :class:`DeadlineExceeded` when the time budget would be blown.
        """
        import random

        schedule = BackoffSchedule(
            self.backoff,
            rng=random.Random(rng_seed) if rng_seed is not None else None,
        )
        predicate = self.predicate
        start = self.clock.monotonic()
        last_exc: BaseException | None = None

        for attempt in range(1, self.max_attempts + 1):
            try:
                return await fn()
            except BaseException as exc:  # noqa: BLE001 - re-raised below unless retryable
                last_exc = exc
                if not predicate(exc):
                    raise
                if attempt >= self.max_attempts:
                    # A single-attempt policy never *retried*: surface the original
                    # error rather than a misleading "exhausted" wrapper. With >1
                    # attempt we genuinely tried and gave up, so RetriesExhausted.
                    if self.max_attempts == 1:
                        raise
                    break
                delay = schedule.next_delay(attempt, retry_after_s=_retry_after(exc))
                elapsed = self.clock.monotonic() - start
                if self.deadline_s is not None and elapsed + delay > self.deadline_s:
                    raise DeadlineExceeded(
                        f"{self.name}: retry deadline {self.deadline_s}s exceeded "
                        f"after {attempt} attempt(s)",
                        attempts=attempt,
                        elapsed_s=elapsed,
                        cause=exc,
                    ) from exc
                self._emit(
                    RetryAttempt(
                        number=attempt, exception=exc, delay_s=delay, elapsed_s=elapsed
                    )
                )
                await self.clock.sleep(delay)

        raise RetriesExhausted(
            f"{self.name}: exhausted {self.max_attempts} attempt(s)",
            attempts=self.max_attempts,
            cause=last_exc,
        ) from last_exc

    def _emit(self, attempt: RetryAttempt) -> None:
        logger.warning(
            "resilience.retry",
            policy=self.name,
            attempt=attempt.number,
            max_attempts=self.max_attempts,
            delay_s=round(attempt.delay_s, 4),
            elapsed_s=round(attempt.elapsed_s, 4),
            error=type(attempt.exception).__name__,
        )
        if self.on_retry is not None:
            try:
                self.on_retry(attempt)
            except Exception:  # pragma: no cover - hook must never break the loop
                logger.exception("resilience.retry.hook_failed", policy=self.name)

    # -- decorator -------------------------------------------------------- #

    def retry(
        self, fn: Callable[..., Awaitable[T]]
    ) -> Callable[..., Awaitable[T]]:
        """Decorate an async function so each call runs under this policy."""

        @functools.wraps(fn)
        async def _wrapped(*args: object, **kwargs: object) -> T:
            return await self.execute(lambda: fn(*args, **kwargs))

        return _wrapped

    # -- context manager -------------------------------------------------- #

    def attempt(self) -> RetryContext:
        """An ``async for`` loop body wrapper — see :class:`RetryContext`."""
        return RetryContext(self)


class RetryContext:
    """An ``async for`` retry loop you can wrap an arbitrary block in.

    Usage::

        async for attempt in policy.attempt():
            with attempt:
                result = await do_the_thing()

    Each iteration yields an :class:`_AttemptGuard`; entering its ``with`` marks the
    try, and an exception raised inside it is captured. If the predicate says retry
    and attempts remain, the loop sleeps the backoff and iterates again; otherwise
    the terminal :class:`RetriesExhausted` / :class:`DeadlineExceeded` / original
    error propagates out of the ``async for``.
    """

    def __init__(self, policy: RetryPolicy, *, rng_seed: int | None = None) -> None:
        import random

        self._policy = policy
        self._schedule = BackoffSchedule(
            policy.backoff,
            rng=random.Random(rng_seed) if rng_seed is not None else None,
        )
        self._predicate = policy.predicate
        self._attempt = 0
        self._start = policy.clock.monotonic()
        self._last_exc: BaseException | None = None
        self._succeeded = False

    def __aiter__(self) -> RetryContext:
        return self

    async def __anext__(self) -> _AttemptGuard:
        # If the previous guard succeeded (no error captured), stop the loop.
        if self._succeeded:
            raise StopAsyncIteration
        # Handle the outcome of the previous attempt (if any).
        if self._attempt > 0:
            exc = self._last_exc
            if exc is None:
                self._succeeded = True
                raise StopAsyncIteration
            if not self._predicate(exc):
                raise exc
            if self._attempt >= self._policy.max_attempts:
                if self._policy.max_attempts == 1:
                    raise exc
                raise RetriesExhausted(
                    f"{self._policy.name}: exhausted {self._policy.max_attempts} attempt(s)",
                    attempts=self._policy.max_attempts,
                    cause=exc,
                ) from exc
            delay = self._schedule.next_delay(self._attempt, retry_after_s=_retry_after(exc))
            elapsed = self._policy.clock.monotonic() - self._start
            if (
                self._policy.deadline_s is not None
                and elapsed + delay > self._policy.deadline_s
            ):
                raise DeadlineExceeded(
                    f"{self._policy.name}: retry deadline {self._policy.deadline_s}s "
                    f"exceeded after {self._attempt} attempt(s)",
                    attempts=self._attempt,
                    elapsed_s=elapsed,
                    cause=exc,
                ) from exc
            self._policy._emit(
                RetryAttempt(
                    number=self._attempt, exception=exc, delay_s=delay, elapsed_s=elapsed
                )
            )
            await self._policy.clock.sleep(delay)
        self._attempt += 1
        self._last_exc = None
        return _AttemptGuard(self)


class _AttemptGuard:
    """A single try, used as ``with attempt:``. Captures a raised exception so the
    surrounding :class:`RetryContext` can decide whether to retry."""

    __slots__ = ("_ctx", "number")

    def __init__(self, ctx: RetryContext) -> None:
        self._ctx = ctx
        self.number = ctx._attempt

    def __enter__(self) -> _AttemptGuard:
        return self

    def __exit__(self, exc_type: object, exc: BaseException | None, tb: object) -> bool:
        if exc is not None and isinstance(exc, BaseException):
            self._ctx._last_exc = exc
            return True  # swallow here; RetryContext re-raises on the next iteration
        return False


def retryable(
    *,
    max_attempts: int = 3,
    backoff: BackoffPolicy | None = None,
    retry_on: RetryOn | None = None,
    deadline_s: float | None = None,
    on_retry: OnRetry | None = None,
    name: str = "retry",
    clock: Clock = SYSTEM_CLOCK,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """A decorator factory mirroring :meth:`RetryPolicy.retry` for inline use::

        @retryable(max_attempts=4, retry_on=TransientError)
        async def fetch(): ...
    """
    policy = RetryPolicy(
        max_attempts=max_attempts,
        backoff=backoff or BackoffPolicy(),
        retry_on=retry_on,
        deadline_s=deadline_s,
        on_retry=on_retry,
        name=name,
        clock=clock,
    )
    return policy.retry


__all__ = [
    "OnRetry",
    "RetryAttempt",
    "RetryContext",
    "RetryOn",
    "RetryPolicy",
    "RetryPredicate",
    "ResilienceError",
    "retryable",
]
