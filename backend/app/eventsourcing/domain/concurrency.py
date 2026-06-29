"""Optimistic-concurrency retry policy for the write side.

When two writers race on the same aggregate, the loser's append raises
:class:`~app.eventsourcing.store.ConcurrencyError`. The correct response is to
**re-load** the aggregate (picking up the winner's events) and **re-decide** the
command against the fresh state, then append again — not to blindly replay the
same stale events. This module owns that loop as a small, deterministic,
injectable policy so the bus stays readable and the backoff is testable.

The policy is pure with respect to time: backoff delays are *returned*, and the
optional sleeper is injected, so tests run with a recording sleeper and assert
the exact delay schedule with zero wall-clock cost.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.eventsourcing.store.protocol import ConcurrencyError

#: An awaitable used to wait between retries (defaults to ``asyncio.sleep``).
Sleeper = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """A bounded exponential-backoff schedule for concurrency retries.

    Attributes:
        max_attempts: total attempts including the first (>= 1). With ``1`` the
            policy never retries; a conflict surfaces immediately.
        base_delay_s: the first backoff delay; each retry multiplies by ``factor``.
        factor: the geometric growth factor between successive delays.
        max_delay_s: a ceiling so a long retry run does not back off unboundedly.
    """

    max_attempts: int = 3
    base_delay_s: float = 0.0
    factor: float = 4.0
    max_delay_s: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0:
            raise ValueError("delays must be non-negative")

    def delay_for(self, attempt: int) -> float:
        """The backoff delay *before* the given 1-based retry attempt.

        ``attempt == 1`` is the first try (no preceding delay -> 0). ``attempt == 2``
        waits ``base_delay_s``, ``attempt == 3`` waits ``base_delay_s * factor``, …,
        each capped at ``max_delay_s``.
        """
        if attempt <= 1:
            return 0.0
        delay = self.base_delay_s * (self.factor ** (attempt - 2))
        return min(delay, self.max_delay_s)


async def _default_sleeper(_delay: float) -> None:  # pragma: no cover - thin shim
    import asyncio

    await asyncio.sleep(_delay)


async def retry_on_conflict(
    operation: Callable[[], Awaitable[_R]],
    *,
    policy: RetryPolicy,
    sleeper: Sleeper | None = None,
) -> _R:
    """Run ``operation`` (load → decide → append), retrying on a concurrency clash.

    ``operation`` must perform a *fresh* load-decide-append each call, so a retry
    re-decides against the winner's events. Any error other than
    :class:`ConcurrencyError` propagates immediately (business-rule failures are
    not retryable).

    Raises:
        ConcurrencyError: the conflict persisted through every attempt.
    """
    sleep = sleeper or _default_sleeper
    last_error: ConcurrencyError | None = None
    for attempt in range(1, policy.max_attempts + 1):
        delay = policy.delay_for(attempt)
        if delay > 0:
            await sleep(delay)
        try:
            return await operation()
        except ConcurrencyError as exc:
            last_error = exc
            continue
    assert last_error is not None  # loop only exits here after a conflict
    raise last_error


# Late type binding (kept simple to avoid a generics-vs-callable mypy snag above).
from typing import TypeVar  # noqa: E402

_R = TypeVar("_R")


__all__ = ["RetryPolicy", "Sleeper", "retry_on_conflict"]
