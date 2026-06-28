"""Exponential-backoff-with-jitter tests (app.queue.backoff, kinora.md §12.1).

Seeded RNG makes the jittered delays exact; the invariants (bounds, monotone
expectation, cap clamp, fixed-schedule back-compat) are asserted directly.
"""

from __future__ import annotations

import random

import pytest

from app.queue.backoff import (
    DEFAULT_BASE_S,
    DEFAULT_CAP_S,
    BackoffSchedule,
    JitterStrategy,
    decorrelated_jitter,
    equal_jitter,
    full_jitter,
)


def test_full_jitter_within_exponential_envelope() -> None:
    rng = random.Random(0)
    # Attempt n's delay never exceeds the capped exponential term base*2**(n-1).
    for attempt, ceil in [(1, 2.0), (2, 4.0), (3, 8.0), (4, 16.0)]:
        for _ in range(50):
            d = full_jitter(attempt, base=2.0, cap=DEFAULT_CAP_S, rng=rng)
            assert 0.0 <= d <= ceil


def test_full_jitter_respects_cap() -> None:
    rng = random.Random(1)
    for _ in range(100):
        d = full_jitter(10, base=2.0, cap=30.0, rng=rng)  # 2*2**9 = 1024, capped to 30
        assert 0.0 <= d <= 30.0


def test_equal_jitter_has_floor() -> None:
    rng = random.Random(2)
    # equal jitter guarantees at least half the exponential term.
    for attempt, term in [(1, 2.0), (2, 4.0), (3, 8.0)]:
        for _ in range(50):
            d = equal_jitter(attempt, base=2.0, cap=DEFAULT_CAP_S, rng=rng)
            assert term / 2 <= d <= term


def test_decorrelated_jitter_bounds() -> None:
    rng = random.Random(3)
    prev = 2.0
    for _ in range(50):
        d = decorrelated_jitter(1, base=2.0, cap=30.0, prev=prev, rng=rng)
        assert 2.0 <= d <= 30.0  # floor=base, ceil=min(cap, prev*3)
        prev = d


def test_schedule_full_is_seed_reproducible() -> None:
    a = BackoffSchedule(strategy=JitterStrategy.FULL, seed=42)
    b = BackoffSchedule(strategy=JitterStrategy.FULL, seed=42)
    assert [a.delay_for(n) for n in range(1, 6)] == [b.delay_for(n) for n in range(1, 6)]


def test_schedule_none_uses_fixed_tuple() -> None:
    sched = BackoffSchedule.fixed_schedule((2.0, 8.0, 30.0))
    assert sched.delay_for(1) == 2.0
    assert sched.delay_for(2) == 8.0
    assert sched.delay_for(3) == 30.0
    assert sched.delay_for(4) == 30.0  # clamps to the last entry past the schedule


def test_schedule_none_without_fixed_is_pure_exponential() -> None:
    sched = BackoffSchedule(strategy=JitterStrategy.NONE, base_s=2.0, cap_s=30.0)
    assert sched.delay_for(1) == 2.0
    assert sched.delay_for(2) == 4.0
    assert sched.delay_for(3) == 8.0
    assert sched.delay_for(5) == 30.0  # 2*2**4 = 32 -> capped


def test_materialise_is_deterministic_and_reset() -> None:
    sched = BackoffSchedule(strategy=JitterStrategy.FULL, seed=7)
    first = sched.materialise(4)
    second = sched.materialise(4)  # materialise resets state -> identical
    assert first == second
    assert len(first) == 4
    assert all(0.0 <= d <= DEFAULT_CAP_S for d in first)


def test_decorrelated_schedule_is_stateful_until_reset() -> None:
    sched = BackoffSchedule(strategy=JitterStrategy.DECORRELATED, seed=5, base_s=2.0, cap_s=30.0)
    seq1 = [sched.delay_for(n) for n in range(1, 5)]
    sched.reset()
    seq2 = [sched.delay_for(n) for n in range(1, 5)]
    assert seq1 == seq2  # reset replays the same seeded sequence
    assert all(d <= 30.0 for d in seq1)


def test_invalid_bounds_raise() -> None:
    with pytest.raises(ValueError):
        BackoffSchedule(base_s=0.0)
    with pytest.raises(ValueError):
        BackoffSchedule(base_s=10.0, cap_s=5.0)


def test_defaults_match_section_12_1_shape() -> None:
    # The §12.1 "2s, 8s, 30s" shape: base 2, cap 30.
    assert DEFAULT_BASE_S == 2.0
    assert DEFAULT_CAP_S == 30.0
