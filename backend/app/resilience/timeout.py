"""A per-attempt timeout wrapper that raises the resilience taxonomy.

A retry loop is only as good as its attempt boundary: without a timeout, one
attempt that hangs forever defeats every other policy (the breaker never sees a
failure, the bulkhead slot is never released, the deadline budget never trips). This
wrapper bounds a single awaited operation and, on expiry, cancels it cleanly and
raises :class:`~app.resilience.errors.CallTimeout` — which the default retry
predicate treats as a retryable per-attempt timeout, so "timeout then retry" is the
natural composition.

It wraps :func:`asyncio.wait_for` (battle-tested cancellation semantics) and
re-labels its :class:`asyncio.TimeoutError` into the taxonomy. ``timeout_s=None``
disables the wrapper (passthrough), so a caller can turn it off without branching.
Because expiry uses the event loop's own timer, timeout tests assert *behaviour*
(an immediately-resolving coro never times out; a coro that awaits an unset event
times out under a tiny real bound) rather than wall-clock duration — no flaky sleeps.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .errors import CallTimeout

T = TypeVar("T")


async def call_with_timeout(
    coro: Awaitable[T],
    timeout_s: float | None,
    *,
    name: str = "call",
) -> T:
    """Await ``coro`` with a per-attempt ceiling.

    Raises :class:`CallTimeout` if it does not complete within ``timeout_s`` (the
    underlying task is cancelled). ``timeout_s=None`` awaits without a ceiling.
    """
    if timeout_s is None:
        return await coro
    if timeout_s <= 0:
        raise ValueError("timeout_s must be > 0 when set")
    try:
        return await asyncio.wait_for(coro, timeout=timeout_s)
    except TimeoutError as exc:
        raise CallTimeout(
            f"{name}: timed out after {timeout_s}s", cause=exc
        ) from exc


def timeout(
    timeout_s: float | None, *, name: str = "call"
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator: bound each call of an async function to ``timeout_s`` seconds."""

    def _decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def _wrapped(*args: object, **kwargs: object) -> T:
            return await call_with_timeout(
                fn(*args, **kwargs), timeout_s, name=name or fn.__name__
            )

        return _wrapped

    return _decorator


__all__ = ["call_with_timeout", "timeout"]
