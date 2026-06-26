"""Unit tests for the pure RetryPolicy (no Redis)."""

from __future__ import annotations

from app.queue.redis_queue import RetryDecision, RetryPolicy

_POLICY = RetryPolicy(cap=2, backoff_s=(2.0, 8.0, 30.0))


def test_retry_until_cap_then_deadletter() -> None:
    assert _POLICY.decide(1) is RetryDecision.RETRY
    assert _POLICY.decide(2) is RetryDecision.RETRY
    assert _POLICY.decide(3) is RetryDecision.DEADLETTER  # past the cap


def test_backoff_follows_schedule_then_clamps_to_last() -> None:
    assert _POLICY.backoff_for(1) == 2.0
    assert _POLICY.backoff_for(2) == 8.0
    assert _POLICY.backoff_for(3) == 30.0
    assert _POLICY.backoff_for(99) == 30.0  # clamps to the final step


def test_empty_backoff_is_zero() -> None:
    assert RetryPolicy(cap=1, backoff_s=()).backoff_for(1) == 0.0
