"""Unit tests for the sliding-window counters (defense.windows)."""

from __future__ import annotations

import pytest

from app.zerotrust.defense.windows import DistinctWindow, SlidingCounter


def test_sliding_counter_prunes_old_events() -> None:
    c = SlidingCounter(window=10.0)
    for i in range(5):
        c.hit(float(i))
    assert c.count(4.0) == 5
    # At t=15 everything before t=5 has aged out (cutoff = 15-10 = 5).
    assert c.count(15.0) == 0
    # At t=12 (cutoff 2) only events at t in (2,12] survive: 3,4.
    c2 = SlidingCounter(window=10.0)
    for i in range(5):
        c2.hit(float(i))
    assert c2.count(12.0) == 2


def test_sliding_counter_rate_per_sec() -> None:
    c = SlidingCounter(window=4.0)
    for _ in range(8):
        c.hit(1.0)
    assert c.rate_per_sec(1.0) == pytest.approx(2.0)


def test_sliding_counter_weight() -> None:
    c = SlidingCounter(window=10.0)
    assert c.hit(0.0, weight=5) == 5


def test_sliding_counter_rejects_bad_window() -> None:
    with pytest.raises(ValueError):
        SlidingCounter(window=0.0)


def test_distinct_window_counts_unique_values() -> None:
    d = DistinctWindow(window=10.0)
    d.add("a", 0.0)
    d.add("b", 1.0)
    d.add("a", 2.0)  # repeat refreshes, does not grow count
    assert d.count(2.0) == 2


def test_distinct_window_expires() -> None:
    d = DistinctWindow(window=5.0)
    d.add("a", 0.0)
    d.add("b", 1.0)
    # At t=7, cutoff=2: 'a'(0) expired, 'b'(1) expired too -> 0.
    assert d.count(7.0) == 0


def test_distinct_window_overflow_saturates() -> None:
    d = DistinctWindow(window=100.0, cap=4)
    for i in range(10):
        d.add(f"v{i}", float(i))
    # 4 in the live map + 6 overflow = 10 distinct still reported.
    assert d.count(9.0) == 10


def test_distinct_window_validation() -> None:
    with pytest.raises(ValueError):
        DistinctWindow(window=0.0)
    with pytest.raises(ValueError):
        DistinctWindow(window=1.0, cap=0)
