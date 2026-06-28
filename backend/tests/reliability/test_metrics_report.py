"""Unit tests for the load-run report (app.reliability.metrics_report)."""

from __future__ import annotations

import pytest

from app.reliability.metrics_report import (
    EndpointStats,
    LoadReport,
    RequestOutcome,
    merge_reports,
)

INTENT = "POST /sessions/{id}/intent"
SEEK = "POST /sessions/{id}/seek"


def _outcome(
    endpoint: str, status: int, ms: float, ok: bool, error: str | None = None
) -> RequestOutcome:
    return RequestOutcome(endpoint=endpoint, status=status, latency_ms=ms, ok=ok, error=error)


def test_endpoint_stats_basic() -> None:
    stats = EndpointStats(INTENT)
    stats.record(_outcome(INTENT, 200, 10.0, True))
    stats.record(_outcome(INTENT, 200, 20.0, True))
    stats.record(_outcome(INTENT, 500, 30.0, False))
    assert stats.total == 3
    assert stats.ok == 2
    assert stats.errors == 1
    assert stats.error_rate == pytest.approx(1 / 3)
    assert stats.status_breakdown == {200: 2, 500: 1}
    assert stats.throughput_rps(3.0) == pytest.approx(1.0)


def test_endpoint_throughput_zero_window() -> None:
    stats = EndpointStats(INTENT)
    stats.record(_outcome(INTENT, 200, 5.0, True))
    assert stats.throughput_rps(0.0) == 0.0


def test_transport_failure_is_status_zero_error() -> None:
    outcome = _outcome(SEEK, 0, 1000.0, False, error="timeout")
    assert outcome.transport_failure is True
    stats = EndpointStats(SEEK)
    stats.record(outcome)
    assert stats.errors == 1
    assert stats.status_breakdown == {0: 1}


def test_load_report_aggregates_across_endpoints() -> None:
    report = LoadReport(wall_seconds=10.0, meta={"target": "http://x", "profile": "steady"})
    report.record_all(
        [
            _outcome(INTENT, 200, 12.0, True),
            _outcome(INTENT, 200, 18.0, True),
            _outcome(INTENT, 429, 5.0, False),
            _outcome(SEEK, 200, 30.0, True),
            _outcome(SEEK, 0, 2000.0, False, error="connect"),
        ]
    )
    assert report.total_requests == 5
    assert report.total_errors == 2
    assert report.error_rate == pytest.approx(0.4)
    assert report.availability == pytest.approx(0.6)
    assert report.throughput_rps == pytest.approx(0.5)
    # Both endpoints are present in the aggregate.
    assert set(report.endpoints) == {INTENT, SEEK}
    overall = report.overall_latency()
    assert overall.count == 5
    assert overall.max_ms == pytest.approx(2000.0, rel=0.05)


def test_load_report_empty() -> None:
    report = LoadReport(wall_seconds=5.0)
    assert report.total_requests == 0
    assert report.error_rate == 0.0
    assert report.availability == 1.0
    assert report.throughput_rps == 0.0
    assert report.overall_latency().count == 0


def test_to_dict_is_serializable_and_sorted() -> None:
    report = LoadReport(wall_seconds=4.0, meta={"target": "http://t"})
    report.record(_outcome(SEEK, 200, 10.0, True))
    report.record(_outcome(INTENT, 200, 5.0, True))
    doc = report.to_dict()
    assert doc["total_requests"] == 2
    assert [e["endpoint"] for e in doc["endpoints"]] == sorted([INTENT, SEEK])
    # Status keys are stringified for JSON.
    assert all(isinstance(k, str) for e in doc["endpoints"] for k in e["status_breakdown"])


def test_render_text_contains_headline_numbers() -> None:
    report = LoadReport(wall_seconds=2.0, meta={"target": "http://t", "profile": "p"})
    report.record(_outcome(INTENT, 200, 10.0, True))
    report.record(_outcome(INTENT, 500, 20.0, False))
    text = report.render_text()
    assert "Kinora load report" in text
    assert "http://t" in text
    assert INTENT in text
    assert "error-rate" in text


def test_merge_reports_combines_workers() -> None:
    a = LoadReport(wall_seconds=10.0, meta={"target": "http://t", "profile": "p"})
    a.record(_outcome(INTENT, 200, 10.0, True))
    a.record(_outcome(INTENT, 200, 20.0, True))
    b = LoadReport(wall_seconds=12.0)
    b.record(_outcome(INTENT, 500, 30.0, False))
    b.record(_outcome(SEEK, 200, 40.0, True))

    merged = merge_reports([a, b])
    # Wall is the max (workers ran concurrently).
    assert merged.wall_seconds == 12.0
    # Meta inherited from the first report.
    assert merged.meta["target"] == "http://t"
    assert merged.total_requests == 4
    assert merged.total_errors == 1
    assert merged.endpoints[INTENT].total == 3
    assert merged.endpoints[SEEK].total == 1
    # Merged latency digest spans all four samples.
    assert merged.overall_latency().count == 4


def test_merge_reports_empty() -> None:
    merged = merge_reports([])
    assert merged.total_requests == 0
    assert merged.wall_seconds == 0.0
