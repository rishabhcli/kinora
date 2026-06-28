"""Unit tests for the retry/backoff policy (no infra)."""

from __future__ import annotations

import random

import pytest

from app.jobs.backoff import DEFAULT_POLICY, BackoffPolicy, RetryDecision


def test_decide_retries_until_cap_then_deadletters() -> None:
    policy = BackoffPolicy(max_attempts=3, jitter=False)
    assert policy.decide(1) is RetryDecision.RETRY
    assert policy.decide(2) is RetryDecision.RETRY
    assert policy.decide(3) is RetryDecision.DEADLETTER
    assert policy.decide(4) is RetryDecision.DEADLETTER


def test_max_attempts_one_never_retries() -> None:
    policy = BackoffPolicy(max_attempts=1, jitter=False)
    assert policy.decide(1) is RetryDecision.DEADLETTER


def test_raw_delay_is_exponential_and_capped() -> None:
    policy = BackoffPolicy(
        max_attempts=10, base_delay_s=2.0, factor=4.0, max_delay_s=300.0, jitter=False
    )
    assert policy.raw_delay_for(1) == 0.0  # first attempt: no delay
    assert policy.raw_delay_for(2) == 2.0
    assert policy.raw_delay_for(3) == 8.0
    assert policy.raw_delay_for(4) == 32.0
    assert policy.raw_delay_for(5) == 128.0
    assert policy.raw_delay_for(6) == 300.0  # 512 capped to 300
    assert policy.raw_delay_for(7) == 300.0


def test_delay_for_without_jitter_equals_raw() -> None:
    policy = BackoffPolicy(jitter=False)
    assert policy.delay_for(3) == policy.raw_delay_for(3)


def test_delay_for_with_jitter_is_in_range_and_seedable() -> None:
    policy = BackoffPolicy(max_attempts=10, base_delay_s=2.0, factor=4.0, jitter=True)
    raw = policy.raw_delay_for(4)  # 32s
    rng = random.Random(42)
    samples = [policy.delay_for(4, rng=rng) for _ in range(100)]
    assert all(0.0 <= s <= raw for s in samples)
    # Deterministic given the seed.
    rng2 = random.Random(42)
    again = [policy.delay_for(4, rng=rng2) for _ in range(100)]
    assert samples == again


def test_first_attempt_delay_zero_even_with_jitter() -> None:
    policy = BackoffPolicy(jitter=True)
    assert policy.delay_for(1) == 0.0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"base_delay_s": -1},
        {"max_delay_s": -1},
        {"factor": 0.5},
    ],
)
def test_invalid_policy_rejected(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(**kwargs)  # type: ignore[arg-type]


def test_default_policy_shape() -> None:
    assert DEFAULT_POLICY.max_attempts == 3
    assert DEFAULT_POLICY.raw_delay_for(2) == 2.0
    assert DEFAULT_POLICY.raw_delay_for(3) == 8.0
