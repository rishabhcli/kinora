"""Tests for app.inference.router.metrics — counters + the P² quantile sketch."""

from __future__ import annotations

import random

import pytest

from app.inference.router.admission import RejectReason
from app.inference.router.metrics import P2Quantile, RouterStats
from app.inference.router.request import RequestPriority


def test_p2_exact_for_small_samples() -> None:
    q = P2Quantile(0.5)
    for x in [3.0, 1.0, 2.0]:
        q.observe(x)
    assert q.value == pytest.approx(2.0)  # median of {1,2,3}


def test_p2_estimates_median_of_uniform() -> None:
    q = P2Quantile(0.5)
    rng = random.Random(0)
    for _ in range(5000):
        q.observe(rng.uniform(0.0, 100.0))
    # Median of U(0,100) is ~50; P² should be close.
    assert 45.0 <= q.value <= 55.0


def test_p2_estimates_p99_of_uniform() -> None:
    q = P2Quantile(0.99)
    rng = random.Random(1)
    for _ in range(20000):
        q.observe(rng.uniform(0.0, 100.0))
    # p99 of U(0,100) is ~99.
    assert 96.0 <= q.value <= 100.0


def test_p2_zero_before_observation() -> None:
    assert P2Quantile(0.5).value == 0.0


def test_p2_rejects_bad_p() -> None:
    with pytest.raises(ValueError):
        P2Quantile(0.0)
    with pytest.raises(ValueError):
        P2Quantile(1.0)


def test_stats_counts_and_snapshot() -> None:
    s = RouterStats()
    s.on_admit()
    s.on_admit()
    s.on_reject(RejectReason.QUEUE_FULL)
    s.on_dispatch(RequestPriority.COMMITTED, 0.2)
    s.on_dispatch(RequestPriority.SPECULATIVE, 0.4)
    s.on_batch(2)
    s.on_complete(ok=True, tokens_in=10, tokens_out=20, cache_hit=False)
    s.on_complete(ok=False, tokens_in=0, tokens_out=0, cache_hit=False)
    snap = s.snapshot()
    assert snap["admitted"] == 2
    assert snap["rejected"] == 1
    assert snap["dispatched"] == 2
    assert snap["succeeded"] == 1
    assert snap["failed"] == 1
    assert snap["tokens_out"] == 20
    assert snap["rejects_by_reason"] == {"queue_full": 1}
    assert snap["served_by_priority"] == {"COMMITTED": 1, "SPECULATIVE": 1}


def test_cancel_and_preempt_counters() -> None:
    s = RouterStats()
    s.on_cancel()
    s.on_cancel()
    s.on_preempt()
    snap = s.snapshot()
    assert snap["cancelled"] == 2
    assert snap["preempted"] == 1


def test_avg_batch_size() -> None:
    s = RouterStats()
    s.on_batch(4)
    s.on_batch(2)
    assert s.avg_batch_size == pytest.approx(3.0)


def test_reject_rate() -> None:
    s = RouterStats()
    for _ in range(3):
        s.on_admit()
    s.on_reject(RejectReason.SHED_LOW_PRIORITY)
    assert s.reject_rate == pytest.approx(0.25)


def test_cache_hit_rate_counts_coalesced() -> None:
    s = RouterStats()
    s.on_dispatch(RequestPriority.COMMITTED, 0.1)
    s.on_dispatch(RequestPriority.COMMITTED, 0.1)
    s.on_dispatch(RequestPriority.COMMITTED, 0.1)
    s.on_coalesce()  # one follower served off a leader
    assert s.cache_hit_rate == pytest.approx(0.25)


def test_wait_quantiles_track_dispatch_waits() -> None:
    s = RouterStats()
    for w in [0.1, 0.2, 0.3, 0.4, 0.5]:
        s.on_dispatch(RequestPriority.COMMITTED, w)
    assert 0.2 <= s.wait_p50_s <= 0.4
