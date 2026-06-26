"""Bounded concurrency + transient-error backoff for coalescing provider calls.

Independent calls (page analysis, identity, keyframe gen) can run concurrently within rate
limits; ``gather_bounded`` / ``map_bounded`` cap the in-flight count with a semaphore while
preserving result order. ``with_backoff`` retries exactly the provider layer's *retryable*
failures (``TransientProviderError`` / ``RateLimited`` — including the known image-model ``429
Throttling.RateQuota``) with exponential backoff, honoring a server-suggested ``retry_after_s``.

The retry predicate keys off the existing ``ProviderError.retryable`` flag (no string-matching),
so it tracks the typed error hierarchy. ``sleep`` is injectable for instant, deterministic tests;
cancellation (``CancelledError`` is a ``BaseException``) is never swallowed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, TypeVar

T = TypeVar("T")
S = TypeVar("S")


def default_should_retry(exc: BaseException) -> bool:
    """Retry iff the provider layer marked the error retryable (``ProviderError.retryable``)."""
    return bool(getattr(exc, "retryable", False))


def _retry_after(exc: BaseException) -> float | None:
    """A non-negative server-suggested wait (``RateLimited.retry_after_s``), else ``None``."""
    val = getattr(exc, "retry_after_s", None)
    if isinstance(val, (int, float)) and val >= 0:
        return float(val)
    return None


async def gather_bounded(awaitables: Iterable[Awaitable[T]], *, limit: int) -> list[T]:
    """Await all ``awaitables`` with at most ``limit`` in flight; results keep input order."""
    if limit <= 0:
        raise ValueError("limit must be a positive integer")
    sem = asyncio.Semaphore(limit)

    async def _run(aw: Awaitable[T]) -> T:
        async with sem:
            return await aw

    results: list[T] = list(await asyncio.gather(*(_run(a) for a in awaitables)))
    return results


async def map_bounded(
    fn: Callable[[S], Awaitable[T]], items: Iterable[S], *, limit: int
) -> list[T]:
    """Apply async ``fn`` to each item, ``limit`` in flight; results keep input order."""
    return await gather_bounded([fn(item) for item in items], limit=limit)


async def with_backoff(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    should_retry: Callable[[BaseException], bool] = default_should_retry,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> T:
    """Call ``fn`` with exponential backoff on retryable errors; re-raise once retries are spent.

    Delay for attempt *n* (1-indexed) is ``min(max_delay, base_delay * 2**(n-1))`` unless the
    error carries ``retry_after_s`` (then that wins). ``retries`` is the number of *extra*
    attempts after the first, so up to ``retries + 1`` calls total.
    """
    attempt = 0
    while True:
        try:
            return await fn()
        except Exception as exc:  # noqa: BLE001 - re-raised unless explicitly retryable
            attempt += 1
            if attempt > retries or not should_retry(exc):
                raise
            delay = _retry_after(exc)
            if delay is None:
                delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            await sleep(delay)


__all__ = [
    "default_should_retry",
    "gather_bounded",
    "map_bounded",
    "with_backoff",
]
