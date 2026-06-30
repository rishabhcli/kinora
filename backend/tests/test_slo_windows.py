"""Deterministic tests for the rolling metric-stream windows (app.slo.windows)."""

from __future__ import annotations

from app.slo.windows import CounterStream, SampleStream, _percentile


def test_counter_ratio_over_window() -> None:
    s = CounterStream(horizon_s=100.0)
    for i in range(10):
        s.record(good=i < 8, now=float(i))  # 8 good, 2 bad
    win = s.window(now=9.0, window_s=100.0)
    assert win.good == 8
    assert win.bad == 2
    assert win.total == 10
    assert win.ratio == 0.8
    assert win.failure_ratio == 0.2


def test_counter_empty_window_is_vacuously_perfect() -> None:
    s = CounterStream(horizon_s=100.0)
    win = s.window(now=50.0, window_s=10.0)
    assert win.total == 0
    assert win.ratio == 1.0  # no failures observed => perfect
    assert win.failure_ratio == 0.0


def test_counter_subwindow_excludes_older_events() -> None:
    s = CounterStream(horizon_s=1000.0)
    s.record(good=False, now=0.0)  # old failure
    s.record(good=True, now=95.0)
    s.record(good=True, now=99.0)
    # A 10s window at now=100 only sees the two recent goods.
    win = s.window(now=100.0, window_s=10.0)
    assert win.total == 2
    assert win.ratio == 1.0


def test_counter_prunes_beyond_horizon() -> None:
    s = CounterStream(horizon_s=10.0)
    s.record(good=True, now=0.0)
    s.record(good=True, now=5.0)
    # Recording at 100 prunes everything older than 90.
    s.record(good=False, now=100.0)
    assert len(s) == 1
    assert s.window(now=100.0, window_s=10.0).bad == 1


def test_counter_weight_records_multiple_events() -> None:
    s = CounterStream(horizon_s=100.0)
    s.record(good=True, now=1.0, weight=5)
    s.record(good=False, now=1.0, weight=5)
    win = s.window(now=1.0, window_s=100.0)
    assert win.total == 10
    assert win.ratio == 0.5


def test_sample_percentiles_nearest_rank() -> None:
    s = SampleStream(horizon_s=1000.0)
    for i in range(1, 101):  # 1..100ms
        s.record(float(i), now=float(i))
    win = s.window(now=100.0, window_s=1000.0)
    assert win.count == 100
    assert win.minimum == 1.0
    assert win.maximum == 100.0
    assert win.p50 == 50.0
    assert win.p95 == 95.0
    assert win.p99 == 99.0
    assert abs(win.mean - 50.5) < 1e-9


def test_sample_empty_window_is_zeroed() -> None:
    s = SampleStream(horizon_s=100.0)
    win = s.window(now=10.0, window_s=5.0)
    assert win.is_empty
    assert win.p95 == 0.0


def test_percentile_edges() -> None:
    vals = [10.0, 20.0, 30.0]
    assert _percentile(vals, 0.0) == 10.0
    assert _percentile(vals, 1.0) == 30.0
    assert _percentile([], 0.5) == 0.0
