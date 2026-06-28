"""Deterministic-bucketing tests — the foundation everything else relies on."""

from __future__ import annotations

import pytest

from app.flags.hashing import (
    TOTAL_BASIS_POINTS,
    bucket_bp,
    bucket_fraction,
    in_rollout,
    weighted_index,
)


def test_bucket_is_in_range() -> None:
    for i in range(1000):
        b = bucket_bp(f"u{i}", "salt")
        assert 0 <= b < TOTAL_BASIS_POINTS


def test_bucket_is_deterministic() -> None:
    assert bucket_bp("alice", "flag-x") == bucket_bp("alice", "flag-x")
    assert bucket_fraction("alice", "flag-x") == bucket_fraction("alice", "flag-x")


def test_bucket_varies_by_salt() -> None:
    # Different salts almost always produce different buckets for the same unit.
    different = sum(
        1 for i in range(500) if bucket_bp(f"u{i}", "a") != bucket_bp(f"u{i}", "b")
    )
    assert different > 490  # overwhelmingly different


def test_bucket_is_uniform() -> None:
    # 20 deciles-ish bins should each get roughly 1/20 of a large sample.
    bins = [0] * 20
    n = 40_000
    for i in range(n):
        b = bucket_bp(f"unit-{i}", "uniformity")
        bins[b * 20 // TOTAL_BASIS_POINTS] += 1
    expected = n / 20
    for count in bins:
        assert abs(count - expected) < expected * 0.15  # within 15% of uniform


def test_in_rollout_edges() -> None:
    assert in_rollout("anyone", "s", 0) is False
    assert in_rollout("anyone", "s", -5) is False
    assert in_rollout("anyone", "s", 100) is True
    assert in_rollout("anyone", "s", 150) is True


def test_in_rollout_is_sticky_as_percentage_grows() -> None:
    # Everyone admitted at p% must still be admitted at q% for q > p (monotone).
    units = [f"user-{i}" for i in range(5000)]
    for lo, hi in [(10, 20), (20, 50), (50, 90)]:
        in_lo = {u for u in units if in_rollout(u, "ramp", lo)}
        in_hi = {u for u in units if in_rollout(u, "ramp", hi)}
        assert in_lo <= in_hi


def test_in_rollout_approximates_percentage() -> None:
    units = [f"u{i}" for i in range(10_000)]
    admitted = sum(1 for u in units if in_rollout(u, "approx", 30))
    assert 0.27 < admitted / len(units) < 0.33


def test_weighted_index_proportional() -> None:
    weights = (5000, 3000, 2000)  # 50/30/20 bp-as-fraction
    counts = [0, 0, 0]
    n = 20_000
    for i in range(n):
        counts[weighted_index(f"w{i}", "split", weights)] += 1
    fr = [c / n for c in counts]
    assert abs(fr[0] - 0.5) < 0.02
    assert abs(fr[1] - 0.3) < 0.02
    assert abs(fr[2] - 0.2) < 0.02


def test_weighted_index_deterministic() -> None:
    weights = (3333, 3333, 3334)
    for i in range(200):
        u = f"d{i}"
        assert weighted_index(u, "s", weights) == weighted_index(u, "s", weights)


def test_weighted_index_rejects_bad_weights() -> None:
    with pytest.raises(ValueError):
        weighted_index("u", "s", ())
    with pytest.raises(ValueError):
        weighted_index("u", "s", (-1, 2))
    with pytest.raises(ValueError):
        weighted_index("u", "s", (0, 0))


def test_weighted_index_single_bucket() -> None:
    assert weighted_index("anything", "s", (10_000,)) == 0


def test_zero_weight_variation_gets_nobody() -> None:
    # A 0-weight middle arm should receive ~no traffic.
    weights = (5000, 0, 5000)
    hits_middle = sum(1 for i in range(5000) if weighted_index(f"z{i}", "s", weights) == 1)
    assert hits_middle == 0
