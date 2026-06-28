"""Retry-on-transient-failure for database operations.

Postgres can fail a transaction for reasons that are *retryable*: a
serialization failure (``40001``) or a deadlock (``40P01``) under SERIALIZABLE /
REPEATABLE READ, or a dropped connection (the pool's ``pool_pre_ping`` catches
most, but a mid-transaction disconnect still surfaces). The correct response is
not to crash the request but to roll back and re-run the whole operation a
bounded number of times with backoff — the standard MVCC retry loop.

This module provides:

* :func:`classify_error` — map an exception to a :class:`RetryClass`
  (``SERIALIZATION`` / ``DEADLOCK`` / ``DISCONNECT`` / ``NON_RETRYABLE``).
* :class:`RetryPolicy` — bounded attempts + exponential backoff with jitter.
* :func:`with_db_retry` — an async decorator/runner that re-invokes a *callable
  that opens its own transaction* on a transient failure. The callable must be
  idempotent at the transaction boundary (it will be run again from scratch), so
  it should take a session factory / unit-of-work, not a live session.

The classifier reads the SQLSTATE from asyncpg / psycopg style exceptions
without importing those drivers (so it stays usable in the hermetic unit suite):
it walks the exception chain looking for a ``sqlstate``/``pgcode`` attribute and
falls back to message sniffing for disconnects.
"""

from __future__ import annotations

import asyncio
import enum
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from sqlalchemy.exc import DBAPIError, DisconnectionError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from app.core.logging import get_logger

logger = get_logger("app.db.retry")

T = TypeVar("T")

# SQLSTATE classes worth retrying.
_SERIALIZATION_FAILURE = "40001"
_DEADLOCK_DETECTED = "40P01"
# Connection-class SQLSTATEs (08xxx) — the server dropped or refused the link.
_CONNECTION_PREFIX = "08"


class RetryClass(enum.StrEnum):
    """How a database error should be treated by the retry loop."""

    SERIALIZATION = "serialization"
    DEADLOCK = "deadlock"
    DISCONNECT = "disconnect"
    NON_RETRYABLE = "non_retryable"

    @property
    def retryable(self) -> bool:
        """True for the transient classes that a re-run can fix."""
        return self is not RetryClass.NON_RETRYABLE


def _sqlstate_of(exc: BaseException) -> str | None:
    """Extract a Postgres SQLSTATE from an exception chain, if present.

    Looks at the exception and its ``__cause__``/``orig`` for a ``sqlstate`` or
    ``pgcode`` attribute (asyncpg uses ``sqlstate``; psycopg uses ``pgcode``).
    """
    seen: set[int] = set()
    stack: list[BaseException | None] = [exc]
    while stack:
        current = stack.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        for attr in ("sqlstate", "pgcode"):
            code = getattr(current, attr, None)
            if isinstance(code, str) and code:
                return code
        # SQLAlchemy wraps the DBAPI error under ``.orig``.
        stack.append(getattr(current, "orig", None))
        stack.append(current.__cause__)
        stack.append(current.__context__)
    return None


def classify_error(exc: BaseException) -> RetryClass:
    """Classify an exception for the retry loop.

    SQLSTATE wins when available; otherwise the SQLAlchemy exception *type* and a
    last-resort message sniff cover driver disconnects that lost their code.
    """
    sqlstate = _sqlstate_of(exc)
    if sqlstate == _SERIALIZATION_FAILURE:
        return RetryClass.SERIALIZATION
    if sqlstate == _DEADLOCK_DETECTED:
        return RetryClass.DEADLOCK
    if sqlstate and sqlstate.startswith(_CONNECTION_PREFIX):
        return RetryClass.DISCONNECT

    if isinstance(exc, DisconnectionError):
        return RetryClass.DISCONNECT
    # An OperationalError without a serialization/deadlock SQLSTATE is most often
    # a transient connection problem (server restart, network blip).
    if isinstance(exc, OperationalError):
        return RetryClass.DISCONNECT
    if isinstance(exc, DBAPIError) and getattr(exc, "connection_invalidated", False):
        return RetryClass.DISCONNECT

    message = str(exc).lower()
    if any(s in message for s in ("could not serialize", "serialization failure")):
        return RetryClass.SERIALIZATION
    if "deadlock detected" in message:
        return RetryClass.DEADLOCK
    if any(
        s in message
        for s in ("connection was closed", "connection is closed", "server closed the connection")
    ):
        return RetryClass.DISCONNECT
    return RetryClass.NON_RETRYABLE


def is_retryable(exc: BaseException) -> bool:
    """Convenience: True when :func:`classify_error` says the error is transient."""
    return classify_error(exc).retryable


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded-attempt exponential backoff with full jitter.

    ``max_attempts`` counts the *total* tries (so ``3`` = one try + two retries).
    Backoff for attempt ``n`` (1-indexed) is
    ``min(max_backoff_s, base_backoff_s * 2**(n-1))`` then jittered uniformly in
    ``[0, that]`` (AWS "full jitter"), which avoids retry stampedes when many
    sessions collide on the same row.
    """

    max_attempts: int = 3
    base_backoff_s: float = 0.05
    max_backoff_s: float = 2.0
    jitter: bool = True
    # Which classes to retry. StaleDataError (optimistic-version conflict) is
    # *not* here by default: a lost update usually means the caller should re-read
    # and decide, not blindly re-run. Opt in via ``retry_stale=True``.
    retry_classes: frozenset[RetryClass] = frozenset(
        {RetryClass.SERIALIZATION, RetryClass.DEADLOCK, RetryClass.DISCONNECT}
    )
    retry_stale: bool = False

    def should_retry(self, exc: BaseException, attempt: int) -> bool:
        """True if ``exc`` on ``attempt`` (1-indexed) is worth another try."""
        if attempt >= self.max_attempts:
            return False
        if self.retry_stale and isinstance(exc, StaleDataError):
            return True
        return classify_error(exc) in self.retry_classes

    def backoff_for(self, attempt: int) -> float:
        """Backoff (seconds) before the ``attempt``-th retry (1-indexed)."""
        raw = min(self.max_backoff_s, self.base_backoff_s * (2 ** (attempt - 1)))
        if self.jitter:
            return random.uniform(0.0, raw)  # noqa: S311 - jitter, not crypto
        return raw


DEFAULT_RETRY_POLICY = RetryPolicy()


async def with_db_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Run ``operation`` with retry-on-transient-failure.

    ``operation`` is a zero-arg async callable that opens and *fully owns* its own
    transaction (e.g. ``lambda: do_work(session_factory)``) — on a transient
    failure it is re-invoked from scratch, so it must not depend on a session that
    was already rolled back. Returns the operation's result; re-raises the last
    error once attempts are exhausted or the error is non-retryable.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001 - re-raised below if not retryable
            if not policy.should_retry(exc, attempt):
                raise
            delay = policy.backoff_for(attempt)
            logger.warning(
                "db.retry",
                attempt=attempt,
                max_attempts=policy.max_attempts,
                kind=classify_error(exc).value,
                backoff_s=round(delay, 4),
            )
            await sleep(delay)


def retrying(
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator form of :func:`with_db_retry` for transaction-owning coroutines.

    The wrapped coroutine must open its own transaction so a retry re-runs it
    cleanly. Arguments are re-passed unchanged on each attempt.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        from functools import wraps

        @wraps(fn)
        async def wrapper(*args: object, **kwargs: object) -> T:
            return await with_db_retry(lambda: fn(*args, **kwargs), policy=policy)

        return wrapper

    return decorator


__all__ = [
    "DEFAULT_RETRY_POLICY",
    "RetryClass",
    "RetryPolicy",
    "classify_error",
    "is_retryable",
    "retrying",
    "with_db_retry",
]
