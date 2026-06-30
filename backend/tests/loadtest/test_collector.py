"""Coordinated-omission correction + throughput / outcome accounting."""

from __future__ import annotations

import pytest

from app.loadtest.collector import LatencyCollector
from app.loadtest.target import LoadResponse, Outcome


def _resp(endpoint: str, latency_s: float, outcome: Outcome = Outcome.OK) -> LoadResponse:
    return LoadResponse(endpoint=endpoint, outcome=outcome, latency_s=latency_s, status=200)


def test_corrected_latency_includes_queueing_delay() -> None:
    """A request dispatched late carries the wait, not just the service time."""
    c = LatencyCollector(correct_omission=False)
    # Intended at t=0, but it actually finished at t=1.0 having taken 0.1 service.
    c.record(_resp("page_turn", 0.1), intended_s=0.0, finish_s=1.0)
    summary = c.corrected_summary(endpoint="page_turn")
    service = c.service_summary(endpoint="page_turn")
    # Corrected p50 reflects the full 1.0 s the user waited; service shows 0.1.
    assert summary.p50 == pytest.approx(1.0, rel=0.02)
    assert service.p50 == pytest.approx(0.1, rel=0.02)


def test_omission_backfill_reconstructs_dropped_tail() -> None:
    """A long stall backfills the requests a naive tool would never record.

    Intended cadence is one request per 0.1 s. A single request intended at t=0
    stalls and finishes at t=1.0. The 9 slots that should have fired during the
    stall (t=0.1 .. 0.9) are backfilled, each with the latency it would have had.
    """
    naive = LatencyCollector(correct_omission=False)
    corrected = LatencyCollector(correct_omission=True)
    interval = 0.1

    for col in (naive, corrected):
        col.record(
            _resp("buffer_state", 1.0),
            intended_s=0.0,
            finish_s=1.0,
            expected_interval_s=interval,
        )

    # Naive: a single 1.0 s sample, no tail reconstruction.
    assert naive.stats_for("buffer_state").counts.ok == 1
    # Corrected: original + 9 backfilled slots = 10 samples.
    cc = corrected.stats_for("buffer_state").counts
    assert cc.ok == 10
    s = corrected.corrected_summary(endpoint="buffer_state")
    # The backfilled latencies range 1.0, 0.9, ... 0.1; median ~0.5.
    assert s.count == 10
    assert s.max == pytest.approx(1.0, rel=0.02)
    assert s.p50 == pytest.approx(0.55, abs=0.1)


def test_no_backfill_when_no_stall() -> None:
    c = LatencyCollector(correct_omission=True)
    # Request finishes well within one interval ⇒ no omitted slots.
    c.record(
        _resp("page_turn", 0.02),
        intended_s=0.0,
        finish_s=0.02,
        expected_interval_s=0.1,
    )
    assert c.stats_for("page_turn").counts.ok == 1


def test_outcome_counts_and_error_rate() -> None:
    c = LatencyCollector(correct_omission=False)
    c.record(_resp("jump", 0.1, Outcome.OK), intended_s=0.0, finish_s=0.1)
    c.record(_resp("jump", 0.1, Outcome.ERROR), intended_s=0.1, finish_s=0.2)
    c.record(_resp("jump", 0.1, Outcome.TIMEOUT), intended_s=0.2, finish_s=0.3)
    c.record_dropped("jump", intended_s=0.3)
    counts = c.stats_for("jump").counts
    assert counts.ok == 1
    assert counts.error == 1
    assert counts.timeout == 1
    assert counts.dropped == 1
    assert counts.total == 4
    assert counts.errors == 3
    assert counts.error_rate == pytest.approx(0.75)


def test_throughput_over_window() -> None:
    c = LatencyCollector(correct_omission=False)
    for i in range(10):
        t = i * 0.5
        c.record(_resp("open_book", 0.05), intended_s=t, finish_s=t + 0.05)
    # 10 requests over a ~4.55 s window.
    assert c.elapsed_s == pytest.approx(4.55, abs=0.01)
    assert c.throughput_rps() == pytest.approx(10 / 4.55, rel=0.02)


def test_aggregate_merges_endpoints() -> None:
    c = LatencyCollector(correct_omission=False)
    c.record(_resp("a", 0.1), intended_s=0.0, finish_s=0.1)
    c.record(_resp("b", 0.2), intended_s=0.1, finish_s=0.3)
    agg = c.aggregate()
    assert agg.counts.total == 2
    assert set(c.endpoints) == {"a", "b"}
