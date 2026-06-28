"""Unit tests for the streaming latency digest (app.reliability.latency)."""

from __future__ import annotations

import math
import random

import pytest

from app.reliability.latency import (
    REL_ERROR,
    LatencyDigest,
    merge_digests,
)


def _exact_quantile_ms(samples: list[float], q: float) -> float:
    """The HdrHistogram-convention exact quantile the digest approximates."""
    ordered = sorted(samples)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def test_empty_digest_is_all_zero() -> None:
    digest = LatencyDigest()
    assert digest.count == 0
    assert digest.min_ms == 0.0
    assert digest.max_ms == 0.0
    assert digest.mean_ms == 0.0
    assert digest.quantile_ms(0.99) == 0.0
    summary = digest.summary()
    assert summary.count == 0
    assert summary.p50_ms == 0.0


def test_single_sample() -> None:
    digest = LatencyDigest()
    digest.record_ms(42.0)
    assert digest.count == 1
    # The reported value is within the relative-error band of the true value.
    assert digest.quantile_ms(0.5) == pytest.approx(42.0, rel=REL_ERROR)
    assert digest.min_ms == 42.0
    assert digest.max_ms == 42.0
    assert digest.mean_ms == 42.0


def test_record_seconds_converts_to_ms() -> None:
    digest = LatencyDigest()
    digest.record_s(0.25)  # 250 ms
    assert digest.quantile_ms(0.5) == pytest.approx(250.0, rel=REL_ERROR)


def test_negative_latency_clamps_to_zero() -> None:
    digest = LatencyDigest()
    digest.record_ms(-5.0)
    assert digest.min_ms == 0.0
    assert digest.quantile_ms(0.5) == pytest.approx(0.01, abs=0.01)


def test_quantile_within_relative_error_of_exact() -> None:
    rng = random.Random(20260628)
    # A right-skewed latency distribution (lognormal-ish), like real render tails.
    samples = [max(0.5, rng.lognormvariate(3.0, 0.8)) for _ in range(50_000)]
    digest = LatencyDigest.from_samples_ms(samples)
    assert digest.count == len(samples)
    for q in (0.5, 0.9, 0.95, 0.99, 0.999):
        approx = digest.quantile_ms(q)
        exact = _exact_quantile_ms(samples, q)
        # Within the documented bucket relative error (plus one bucket of slack).
        assert approx == pytest.approx(exact, rel=2 * REL_ERROR + 1e-9), q


def test_min_max_mean_exact() -> None:
    samples = [1.0, 2.0, 3.0, 4.0, 100.0]
    digest = LatencyDigest.from_samples_ms(samples)
    assert digest.min_ms == 1.0
    assert digest.max_ms == 100.0
    assert digest.mean_ms == pytest.approx(sum(samples) / len(samples))


def test_quantile_q_out_of_range_raises() -> None:
    digest = LatencyDigest()
    digest.record_ms(1.0)
    with pytest.raises(ValueError):
        digest.quantile_ms(1.5)
    with pytest.raises(ValueError):
        digest.quantile_ms(-0.1)


def test_merge_is_associative_and_correct() -> None:
    rng = random.Random(7)
    a_samples = [rng.uniform(1, 50) for _ in range(3000)]
    b_samples = [rng.uniform(40, 200) for _ in range(2000)]
    c_samples = [rng.uniform(5, 500) for _ in range(1000)]

    a = LatencyDigest.from_samples_ms(a_samples)
    b = LatencyDigest.from_samples_ms(b_samples)
    c = LatencyDigest.from_samples_ms(c_samples)

    left = a.merge(b).merge(c)
    right = a.merge(b.merge(c))
    combined = LatencyDigest.from_samples_ms(a_samples + b_samples + c_samples)

    assert left.count == combined.count == 6000
    assert right.count == 6000
    # Associativity: both groupings produce the same percentile estimates.
    for q in (0.5, 0.9, 0.99):
        assert left.quantile_ms(q) == right.quantile_ms(q)
    # The merge matches building one digest from all samples (same histogram).
    for q in (0.5, 0.9, 0.99):
        assert left.quantile_ms(q) == combined.quantile_ms(q)
    assert left.min_ms == combined.min_ms
    assert left.max_ms == combined.max_ms


def test_merge_with_empty_is_identity() -> None:
    a = LatencyDigest.from_samples_ms([10.0, 20.0, 30.0])
    empty = LatencyDigest()
    merged = a.merge(empty)
    assert merged.count == 3
    assert merged.min_ms == 10.0
    assert merged.max_ms == 30.0
    # Merging empty into empty stays empty.
    assert empty.merge(LatencyDigest()).count == 0


def test_merge_digests_helper() -> None:
    digests = [LatencyDigest.from_samples_ms([float(i)]) for i in range(1, 11)]
    out = merge_digests(digests)
    assert out.count == 10
    assert out.min_ms == 1.0
    assert out.max_ms == 10.0


def test_merge_in_place_matches_merge() -> None:
    a = LatencyDigest.from_samples_ms([1.0, 2.0, 3.0])
    b = LatencyDigest.from_samples_ms([4.0, 5.0, 6.0])
    in_place = LatencyDigest.from_samples_ms([1.0, 2.0, 3.0])
    in_place.merge_in_place(b)
    pure = a.merge(b)
    assert in_place.count == pure.count
    assert in_place.quantile_ms(0.5) == pure.quantile_ms(0.5)


def test_monotone_buckets_preserve_quantile_order() -> None:
    digest = LatencyDigest.from_samples_ms([float(i) for i in range(1, 1001)])
    p50 = digest.quantile_ms(0.5)
    p90 = digest.quantile_ms(0.9)
    p99 = digest.quantile_ms(0.99)
    assert p50 <= p90 <= p99
