"""Tests for the injected clock and the error taxonomy (no infra, instant)."""

from __future__ import annotations

import asyncio

import pytest

from app.resilience.clock import SYSTEM_CLOCK, ManualClock, SystemClock
from app.resilience.errors import (
    AuthError,
    CallTimeout,
    ChaosInjectedError,
    CircuitOpen,
    DeadlineExceeded,
    PermanentError,
    RateLimitedError,
    ResilienceError,
    RetriesExhausted,
    TransientError,
    classify_exception,
)

# --------------------------------------------------------------------------- #
# ManualClock
# --------------------------------------------------------------------------- #


def test_manual_clock_advances_only_explicitly() -> None:
    clock = ManualClock(start=100.0)
    assert clock.time() == 100.0
    assert clock.monotonic() == 0.0
    clock.advance(5.0)
    assert clock.time() == 105.0
    assert clock.monotonic() == 5.0


def test_manual_clock_rejects_backwards() -> None:
    clock = ManualClock()
    with pytest.raises(ValueError):
        clock.advance(-1.0)


async def test_manual_clock_sleep_advances_and_records() -> None:
    clock = ManualClock(start=0.0)
    await clock.sleep(2.5)
    await clock.sleep(0.5)
    assert clock.slept == [2.5, 0.5]
    assert clock.monotonic() == 3.0


async def test_manual_clock_sleep_clamps_negative_to_zero() -> None:
    clock = ManualClock(start=0.0)
    await clock.sleep(-3.0)
    assert clock.slept == [0.0]
    assert clock.monotonic() == 0.0


def test_system_clock_is_monotonic_and_shared() -> None:
    assert isinstance(SYSTEM_CLOCK, SystemClock)
    a = SYSTEM_CLOCK.monotonic()
    b = SYSTEM_CLOCK.monotonic()
    assert b >= a


async def test_system_clock_sleep_zero_is_instant() -> None:
    await asyncio.wait_for(SYSTEM_CLOCK.sleep(0.0), timeout=1.0)


# --------------------------------------------------------------------------- #
# Error taxonomy
# --------------------------------------------------------------------------- #


def test_retryable_flags_are_right() -> None:
    assert TransientError("x").retryable is True
    assert RateLimitedError("x").retryable is True
    assert ChaosInjectedError("x").retryable is True
    assert CircuitOpen("x").retryable is True
    assert PermanentError("x").retryable is False
    assert AuthError("x").retryable is False


def test_retryable_override_per_instance() -> None:
    assert ResilienceError("x", retryable=True).retryable is True
    assert TransientError("x", retryable=False).retryable is False


def test_rate_limited_carries_retry_after() -> None:
    err = RateLimitedError("throttled", retry_after_s=2.5)
    assert err.retry_after_s == 2.5


def test_str_includes_cause_when_distinct() -> None:
    cause = ValueError("boom")
    err = RetriesExhausted("gave up", attempts=3, cause=cause)
    assert "gave up" in str(err)
    assert "ValueError" in str(err)
    assert "boom" in str(err)


def test_deadline_exceeded_carries_metadata() -> None:
    err = DeadlineExceeded("late", attempts=2, elapsed_s=1.5, cause=TimeoutError())
    assert err.attempts == 2
    assert err.elapsed_s == 1.5


def test_classify_resilience_error_uses_own_flag() -> None:
    assert classify_exception(TransientError("x")) is True
    assert classify_exception(PermanentError("x")) is False


def test_classify_stdlib_transient() -> None:
    assert classify_exception(TimeoutError()) is True
    assert classify_exception(ConnectionError()) is True
    assert classify_exception(ConnectionResetError()) is True


def test_classify_unknown_is_not_retryable() -> None:
    assert classify_exception(ValueError("bug")) is False
    assert classify_exception(KeyError("k")) is False


def test_classify_provider_error_respects_its_retryable() -> None:
    from app.providers.errors import ProviderBadRequest, TransientProviderError

    assert classify_exception(TransientProviderError("blip")) is True
    assert classify_exception(ProviderBadRequest("bad")) is False


def test_call_timeout_is_a_transient_timeout() -> None:
    err = CallTimeout("slow")
    assert err.retryable is True
    assert classify_exception(err) is True
