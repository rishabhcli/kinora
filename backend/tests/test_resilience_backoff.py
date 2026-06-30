"""Deterministic tests for the resilience backoff schedule (no infra, no sleep).

Jitter bounds are asserted under a seeded RNG so the exact sequence is fixed, and
the exponential envelope / Retry-After folding are checked directly.
"""

from __future__ import annotations

import random

import pytest

from app.resilience.backoff import BackoffPolicy, BackoffSchedule, JitterStrategy


def test_policy_validation() -> None:
    with pytest.raises(ValueError):
        BackoffPolicy(base_s=0.0)
    with pytest.raises(ValueError):
        BackoffPolicy(base_s=1.0, max_s=0.5)
    with pytest.raises(ValueError):
        BackoffPolicy(multiplier=0.9)
    with pytest.raises(ValueError):
        BackoffPolicy(retry_after_cap_s=-1.0)


def test_exp_cap_grows_then_clamps() -> None:
    sched = BackoffSchedule(BackoffPolicy(base_s=1.0, max_s=8.0, multiplier=2.0))
    assert sched.exp_cap(1) == 1.0
    assert sched.exp_cap(2) == 2.0
    assert sched.exp_cap(3) == 4.0
    assert sched.exp_cap(4) == 8.0
    assert sched.exp_cap(5) == 8.0  # clamped to max_s
    assert sched.exp_cap(50) == 8.0


def test_none_strategy_is_deterministic_exponential() -> None:
    sched = BackoffSchedule(
        BackoffPolicy(base_s=0.5, max_s=100.0, strategy=JitterStrategy.NONE)
    )
    assert sched.next_delay(1) == 0.5
    assert sched.next_delay(2) == 1.0
    assert sched.next_delay(3) == 2.0


def test_full_jitter_within_bounds_for_every_attempt() -> None:
    rng = random.Random(1234)
    policy = BackoffPolicy(base_s=0.1, max_s=10.0, strategy=JitterStrategy.FULL)
    sched = BackoffSchedule(policy, rng=rng)
    for attempt in range(1, 12):
        cap = sched.exp_cap(attempt)
        delay = sched.next_delay(attempt)
        assert 0.0 <= delay <= cap + 1e-9


def test_equal_jitter_keeps_a_floor_of_half_the_cap() -> None:
    rng = random.Random(99)
    policy = BackoffPolicy(base_s=1.0, max_s=64.0, strategy=JitterStrategy.EQUAL)
    sched = BackoffSchedule(policy, rng=rng)
    for attempt in range(1, 8):
        cap = sched.exp_cap(attempt)
        delay = sched.next_delay(attempt)
        assert cap / 2.0 - 1e-9 <= delay <= cap + 1e-9


def test_decorrelated_jitter_walks_within_self_clocked_bounds() -> None:
    rng = random.Random(7)
    policy = BackoffPolicy(base_s=0.5, max_s=20.0, strategy=JitterStrategy.DECORRELATED)
    sched = BackoffSchedule(policy, rng=rng)
    prev = policy.base_s
    for attempt in range(1, 15):
        delay = sched.next_delay(attempt)
        assert policy.base_s - 1e-9 <= delay <= min(prev * 3.0, policy.max_s) + 1e-9
        assert delay <= policy.max_s + 1e-9
        prev = max(delay, policy.base_s)


def test_seeded_rng_is_reproducible() -> None:
    p = BackoffPolicy(strategy=JitterStrategy.FULL)
    a = [BackoffSchedule(p, rng=random.Random(5)).next_delay(i) for i in range(1, 6)]
    b = [BackoffSchedule(p, rng=random.Random(5)).next_delay(i) for i in range(1, 6)]
    assert a == b


def test_retry_after_acts_as_floor_and_is_capped() -> None:
    policy = BackoffPolicy(
        base_s=0.1,
        max_s=1.0,
        strategy=JitterStrategy.FULL,
        retry_after_cap_s=5.0,
    )
    sched = BackoffSchedule(policy, rng=random.Random(0))
    # Server asks for 3s; jitter (<= cap=~1s) can only push it higher.
    delay = sched.next_delay(1, retry_after_s=3.0)
    assert delay >= 3.0
    # A hostile huge Retry-After is clamped to the cap.
    delay = sched.next_delay(1, retry_after_s=10_000.0)
    assert delay <= 5.0 + 1.0  # cap + max jitter headroom


def test_retry_after_ignored_when_policy_disables_it() -> None:
    policy = BackoffPolicy(
        base_s=0.1, max_s=0.1, strategy=JitterStrategy.NONE, respect_retry_after=False
    )
    sched = BackoffSchedule(policy)
    assert sched.next_delay(1, retry_after_s=99.0) == pytest.approx(0.1)


def test_reset_restarts_decorrelated_walk() -> None:
    policy = BackoffPolicy(strategy=JitterStrategy.DECORRELATED)
    sched = BackoffSchedule(policy, rng=random.Random(3))
    for i in range(1, 6):
        sched.next_delay(i)
    sched.reset()
    # After reset the walk starts from base again (upper bound = base*3).
    first = sched.next_delay(1)
    assert policy.base_s - 1e-9 <= first <= policy.base_s * 3.0 + 1e-9
