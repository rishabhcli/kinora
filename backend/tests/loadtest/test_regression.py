"""Regression detection vs a baseline + baseline JSON round-trip."""

from __future__ import annotations

import pytest

from app.loadtest.collector import LatencyCollector
from app.loadtest.regression import (
    Baseline,
    Tolerance,
    detect_regressions,
)
from app.loadtest.target import LoadResponse, Outcome


def _collector(endpoint: str, latency_s: float, n: int, *, errors: int = 0) -> LatencyCollector:
    c = LatencyCollector(correct_omission=False)
    for i in range(n):
        t = i * 0.001
        c.record(
            LoadResponse(endpoint=endpoint, outcome=Outcome.OK, latency_s=latency_s, status=200),
            intended_s=t,
            finish_s=t + latency_s,
        )
    for i in range(errors):
        t = (n + i) * 0.001
        c.record(
            LoadResponse(endpoint=endpoint, outcome=Outcome.ERROR, latency_s=latency_s, status=500),
            intended_s=t,
            finish_s=t + latency_s,
        )
    return c


def test_no_regression_when_metrics_stable() -> None:
    base_c = _collector("page_turn", 0.05, 500)
    baseline = Baseline.from_collector(base_c, label="good")
    # A near-identical run.
    current = _collector("page_turn", 0.051, 500)
    report = detect_regressions(current, baseline)
    assert not report.regressed
    assert not report.findings


def test_latency_regression_flagged() -> None:
    base_c = _collector("page_turn", 0.05, 500)
    baseline = Baseline.from_collector(base_c, label="good")
    # p99 doubles — a clear regression beyond 15% relative + 10 ms absolute.
    current = _collector("page_turn", 0.12, 500)
    report = detect_regressions(current, baseline)
    assert report.regressed
    assert any(f.metric.startswith("p") for f in report.findings)
    finding = next(f for f in report.findings if f.metric == "p99")
    assert finding.current > finding.baseline


def test_small_change_under_absolute_floor_is_quiet() -> None:
    """A 1 ms→1.5 ms change is +50% relative but below the abs floor — no flag."""
    base_c = _collector("buffer_state", 0.001, 500)
    baseline = Baseline.from_collector(base_c, label="good")
    current = _collector("buffer_state", 0.0015, 500)
    report = detect_regressions(
        current, baseline, tolerance=Tolerance(rel_tol=0.15, abs_tol_s=0.010, require_both=True)
    )
    assert not report.regressed


def test_error_rate_regression_flagged() -> None:
    base_c = _collector("jump", 0.05, 1000, errors=0)
    baseline = Baseline.from_collector(base_c, label="good")
    current = _collector("jump", 0.05, 950, errors=50)  # 5% error rate now
    report = detect_regressions(current, baseline)
    assert report.regressed
    assert any(f.metric == "error_rate" for f in report.findings)


def test_new_and_missing_endpoints_reported() -> None:
    base_c = _collector("page_turn", 0.05, 100)
    baseline = Baseline.from_collector(base_c, label="good")
    current = _collector("comment", 0.05, 100)  # different endpoint entirely
    report = detect_regressions(current, baseline)
    assert "comment" in report.new_endpoints
    assert "page_turn" in report.missing_endpoints


def test_baseline_json_round_trip() -> None:
    base_c = _collector("page_turn", 0.05, 300, errors=3)
    baseline = Baseline.from_collector(base_c, label="run-42")
    restored = Baseline.from_dict(baseline.to_dict())
    assert restored.label == "run-42"
    assert set(restored.endpoints) == set(baseline.endpoints)
    eb = restored.endpoints["page_turn"]
    orig = baseline.endpoints["page_turn"]
    assert eb.latency["p99"] == pytest.approx(orig.latency["p99"])
    assert eb.error_rate == pytest.approx(orig.error_rate)


def test_require_both_false_flags_on_relative_alone() -> None:
    base_c = _collector("x", 0.001, 500)
    baseline = Baseline.from_collector(base_c, label="good")
    current = _collector("x", 0.0015, 500)
    report = detect_regressions(
        current, baseline, tolerance=Tolerance(rel_tol=0.15, abs_tol_s=0.010, require_both=False)
    )
    assert report.regressed  # +50% relative trips it even though abs is tiny
