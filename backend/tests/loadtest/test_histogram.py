"""Percentile accuracy on known distributions + mergeability of the histogram."""

from __future__ import annotations

import random

import pytest

from app.loadtest.histogram import (
    REL_ERROR,
    LatencyHistogram,
    exact_percentile,
)


def test_percentiles_match_exact_within_rel_error_uniform() -> None:
    rng = random.Random(1)
    values = [rng.uniform(0.001, 2.0) for _ in range(20_000)]
    h = LatencyHistogram()
    h.record_all(values)
    for q in (50, 90, 95, 99, 99.9):
        est = h.percentile(q)
        exact = exact_percentile(values, q)
        # Estimate is within the histogram's documented relative error band.
        assert est == pytest.approx(exact, rel=3 * REL_ERROR)


def test_percentiles_on_constant_distribution_are_exact_ish() -> None:
    h = LatencyHistogram()
    for _ in range(1000):
        h.record(0.05)  # 50 ms
    s = h.summary()
    assert s.count == 1000
    for v in (s.p50, s.p90, s.p95, s.p99):
        assert v == pytest.approx(0.05, rel=REL_ERROR)


def test_extremes_use_true_min_max_not_bucket() -> None:
    h = LatencyHistogram()
    h.record(0.001)
    h.record(5.0)
    h.record(0.5)
    # 0th / 100th percentile return the exact tracked extrema.
    assert h.percentile(0) == pytest.approx(0.001)
    assert h.percentile(100) == pytest.approx(5.0)
    assert h.min == pytest.approx(0.001)
    assert h.max == pytest.approx(5.0)


def test_merge_is_associative_and_count_preserving() -> None:
    rng = random.Random(7)
    a = LatencyHistogram()
    b = LatencyHistogram()
    c = LatencyHistogram()
    for _ in range(500):
        a.record(rng.random())
    for _ in range(700):
        b.record(rng.random())
    for _ in range(300):
        c.record(rng.random())

    left = a.merge(b).merge(c)
    right = a.merge(b.merge(c))
    assert left.count == right.count == 1500
    # Associativity: same percentiles either way (same bins, same order).
    for q in (50, 90, 99):
        assert left.percentile(q) == pytest.approx(right.percentile(q))


def test_merge_in_matches_recording_union() -> None:
    rng = random.Random(11)
    pool_a = [rng.random() for _ in range(400)]
    pool_b = [rng.random() for _ in range(600)]

    merged = LatencyHistogram()
    ha = LatencyHistogram()
    ha.record_all(pool_a)
    hb = LatencyHistogram()
    hb.record_all(pool_b)
    merged.merge_in(ha)
    merged.merge_in(hb)

    union = LatencyHistogram()
    union.record_all(pool_a + pool_b)

    assert merged.count == union.count
    assert merged.mean == pytest.approx(union.mean, rel=1e-9)
    for q in (50, 95, 99):
        assert merged.percentile(q) == pytest.approx(union.percentile(q))


def test_empty_histogram_is_zeroed() -> None:
    h = LatencyHistogram()
    s = h.summary()
    assert s.count == 0
    assert s.p50 == 0.0 and s.max == 0.0 and s.mean == 0.0


def test_bins_round_trip() -> None:
    rng = random.Random(3)
    h = LatencyHistogram()
    for _ in range(1000):
        h.record(rng.expovariate(2.0))
    restored = LatencyHistogram.from_bins(
        h.to_bins(), total_sum=h.mean * h.count, min_v=h.min, max_v=h.max
    )
    assert restored.count == h.count
    for q in (50, 90, 99):
        assert restored.percentile(q) == pytest.approx(h.percentile(q))
