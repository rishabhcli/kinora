"""Unit tests for the transient-error classifier and the retry loop (no infra)."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DisconnectionError, OperationalError
from sqlalchemy.orm.exc import StaleDataError

from app.db.retry import (
    RetryClass,
    RetryPolicy,
    classify_error,
    is_retryable,
    retrying,
    with_db_retry,
)


class _FakePgError(Exception):
    """Mimics an asyncpg error carrying a SQLSTATE on ``sqlstate``."""

    def __init__(self, sqlstate: str, message: str = "boom") -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


def test_classify_serialization_and_deadlock_by_sqlstate() -> None:
    assert classify_error(_FakePgError("40001")) is RetryClass.SERIALIZATION
    assert classify_error(_FakePgError("40P01")) is RetryClass.DEADLOCK
    assert classify_error(_FakePgError("08006")) is RetryClass.DISCONNECT


def test_classify_walks_exception_chain() -> None:
    inner = _FakePgError("40001")
    outer = RuntimeError("wrapped")
    outer.__cause__ = inner
    assert classify_error(outer) is RetryClass.SERIALIZATION


def test_classify_orig_attribute() -> None:
    # SQLAlchemy attaches the DBAPI error under .orig.
    err = OperationalError("stmt", {}, _FakePgError("40P01"))
    assert classify_error(err) is RetryClass.DEADLOCK


def test_classify_disconnect_types() -> None:
    assert classify_error(DisconnectionError("gone")) is RetryClass.DISCONNECT
    # An OperationalError without a transient SQLSTATE is treated as a disconnect.
    assert classify_error(OperationalError("stmt", {}, Exception("x"))) is RetryClass.DISCONNECT


def test_classify_message_fallback() -> None:
    assert classify_error(Exception("could not serialize access")) is RetryClass.SERIALIZATION
    assert classify_error(Exception("deadlock detected")) is RetryClass.DEADLOCK
    assert classify_error(Exception("server closed the connection")) is RetryClass.DISCONNECT


def test_classify_non_retryable() -> None:
    assert classify_error(ValueError("bad value")) is RetryClass.NON_RETRYABLE
    assert is_retryable(ValueError("nope")) is False
    assert is_retryable(_FakePgError("40001")) is True


def test_policy_should_retry_respects_attempt_cap() -> None:
    policy = RetryPolicy(max_attempts=3)
    err = _FakePgError("40001")
    assert policy.should_retry(err, attempt=1) is True
    assert policy.should_retry(err, attempt=2) is True
    assert policy.should_retry(err, attempt=3) is False  # last attempt


def test_policy_stale_data_opt_in() -> None:
    stale = StaleDataError("lost update")
    assert RetryPolicy().should_retry(stale, attempt=1) is False
    assert RetryPolicy(retry_stale=True).should_retry(stale, attempt=1) is True


def test_policy_backoff_no_jitter_is_exponential() -> None:
    policy = RetryPolicy(base_backoff_s=0.1, max_backoff_s=10.0, jitter=False)
    assert policy.backoff_for(1) == 0.1
    assert policy.backoff_for(2) == 0.2
    assert policy.backoff_for(3) == 0.4


def test_policy_backoff_caps_at_max() -> None:
    policy = RetryPolicy(base_backoff_s=1.0, max_backoff_s=2.0, jitter=False)
    assert policy.backoff_for(10) == 2.0


def test_policy_backoff_jitter_within_bounds() -> None:
    policy = RetryPolicy(base_backoff_s=1.0, max_backoff_s=4.0, jitter=True)
    for attempt in range(1, 5):
        cap = min(4.0, 1.0 * (2 ** (attempt - 1)))
        for _ in range(20):
            assert 0.0 <= policy.backoff_for(attempt) <= cap


async def test_with_db_retry_succeeds_after_transient() -> None:
    calls = {"n": 0}

    async def op() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakePgError("40001")
        return "ok"

    async def no_sleep(_: float) -> None:
        return None

    result = await with_db_retry(
        op, policy=RetryPolicy(max_attempts=5, jitter=False, base_backoff_s=0), sleep=no_sleep
    )
    assert result == "ok"
    assert calls["n"] == 3


async def test_with_db_retry_reraises_non_retryable() -> None:
    async def op() -> None:
        raise ValueError("permanent")

    with pytest.raises(ValueError, match="permanent"):
        await with_db_retry(op)


async def test_with_db_retry_exhausts_attempts() -> None:
    calls = {"n": 0}

    async def op() -> None:
        calls["n"] += 1
        raise _FakePgError("40P01")

    async def no_sleep(_: float) -> None:
        return None

    with pytest.raises(_FakePgError):
        await with_db_retry(
            op, policy=RetryPolicy(max_attempts=4, base_backoff_s=0, jitter=False), sleep=no_sleep
        )
    assert calls["n"] == 4


async def test_retrying_decorator() -> None:
    attempts = {"n": 0}

    @retrying(RetryPolicy(max_attempts=3, base_backoff_s=0, jitter=False))
    async def flaky(x: int) -> int:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _FakePgError("40001")
        return x * 2

    # The decorator's default sleep is asyncio.sleep(0.0) on a zero backoff.
    assert await flaky(21) == 42
    assert attempts["n"] == 2
