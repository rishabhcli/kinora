"""Unit tests for SLOs / error budgets / burn rate (app.reliability.slo)."""

from __future__ import annotations

import math

import pytest

from app.reliability.metrics_report import LoadReport, RequestOutcome
from app.reliability.scenarios import EP_INTENT, EP_SEEK
from app.reliability.slo import (
    SLO,
    BurnRateWindow,
    MultiWindowBurnAlert,
    SLIKind,
    SLOSet,
    burn_rate,
    default_kinora_slos,
    error_budget,
)


def test_availability_slo_lower_bound() -> None:
    slo = SLO("avail", SLIKind.AVAILABILITY, 0.99)
    assert slo.is_lower_bound is True
    met = slo.evaluate(0.995)
    assert met.met is True
    assert met.margin == pytest.approx(0.005)
    missed = slo.evaluate(0.98)
    assert missed.met is False
    assert missed.margin < 0


def test_latency_slo_upper_bound() -> None:
    slo = SLO("p99", SLIKind.LATENCY_P99, 250.0)
    assert slo.is_lower_bound is False
    assert slo.evaluate(200.0).met is True
    assert slo.evaluate(300.0).met is False


def test_error_rate_slo_upper_bound() -> None:
    slo = SLO("err", SLIKind.ERROR_RATE, 0.01)
    assert slo.evaluate(0.005).met is True
    assert slo.evaluate(0.02).met is False


def _report_with(intent_ms: float, seek_ms: float, errors: int, ok: int) -> LoadReport:
    report = LoadReport(wall_seconds=10.0)
    for _ in range(ok):
        report.record(RequestOutcome(EP_INTENT, 200, intent_ms, True))
    for _ in range(errors):
        report.record(RequestOutcome(EP_INTENT, 500, intent_ms, False))
    report.record(RequestOutcome(EP_SEEK, 200, seek_ms, True))
    return report


def test_slo_set_evaluates_report_pass() -> None:
    report = _report_with(intent_ms=50.0, seek_ms=30.0, errors=0, ok=100)
    verdict = default_kinora_slos().evaluate_report(report)
    assert verdict.passed is True
    assert verdict.violations == []


def test_slo_set_evaluates_report_fail_on_latency() -> None:
    report = _report_with(intent_ms=400.0, seek_ms=30.0, errors=0, ok=100)
    verdict = default_kinora_slos(intent_p99_ms=250.0).evaluate_report(report)
    assert verdict.passed is False
    names = {v.slo.name for v in verdict.violations}
    assert "intent-p99" in names


def test_slo_set_evaluates_report_fail_on_availability() -> None:
    report = _report_with(intent_ms=50.0, seek_ms=30.0, errors=50, ok=50)
    verdict = default_kinora_slos(availability=0.995).evaluate_report(report)
    assert verdict.passed is False
    assert any(v.slo.name == "availability" for v in verdict.violations)


def test_missing_endpoint_meets_vacuously() -> None:
    # A report with no seek traffic still passes the seek SLO (vacuous).
    report = LoadReport(wall_seconds=5.0)
    report.record(RequestOutcome(EP_INTENT, 200, 10.0, True))
    verdict = SLOSet(
        slos=(SLO("seek-p99", SLIKind.LATENCY_P99, 150.0, endpoint=EP_SEEK),)
    ).evaluate_report(report)
    assert verdict.passed is True


def test_verdict_render_and_dict() -> None:
    report = _report_with(intent_ms=400.0, seek_ms=30.0, errors=0, ok=10)
    verdict = default_kinora_slos().evaluate_report(report)
    text = verdict.render_text()
    assert "SLO verdict: FAIL" in text
    assert "intent-p99" in text
    doc = verdict.to_dict()
    assert doc["passed"] is False
    assert isinstance(doc["results"], list)


def test_error_budget() -> None:
    assert error_budget(0.999) == pytest.approx(0.001)
    assert error_budget(1.0) == 0.0
    with pytest.raises(ValueError):
        error_budget(1.5)


def test_burn_rate() -> None:
    # On-budget burn is exactly 1.0.
    assert burn_rate(0.001, 0.999) == pytest.approx(1.0)
    # Double the allowed error rate => 2x burn.
    assert burn_rate(0.002, 0.999) == pytest.approx(2.0)
    # No budget, no errors => 0; no budget, some errors => infinite.
    assert burn_rate(0.0, 1.0) == 0.0
    assert math.isinf(burn_rate(0.01, 1.0))


def test_multi_window_burn_alert_requires_agreement() -> None:
    target = 0.999  # 0.1% budget
    # Fast window sees a big spike; slow window sees almost nothing.
    fast = BurnRateWindow("5m", observed_error_rate=0.02, threshold=14.4)
    slow = BurnRateWindow("1h", observed_error_rate=0.0005, threshold=14.4)
    alert = MultiWindowBurnAlert(target_availability=target, windows=(fast, slow))
    # Fast fires (20x burn) but slow does not (0.5x) => no page.
    assert fast.fires(target) is True
    assert slow.fires(target) is False
    assert alert.fires() is False

    # Both windows hot => page.
    slow_hot = BurnRateWindow("1h", observed_error_rate=0.02, threshold=14.4)
    alert_hot = MultiWindowBurnAlert(target_availability=target, windows=(fast, slow_hot))
    assert alert_hot.fires() is True


def test_multi_window_no_windows_does_not_fire() -> None:
    assert MultiWindowBurnAlert(target_availability=0.99, windows=()).fires() is False


def test_slos_from_settings_reads_config() -> None:
    from dataclasses import dataclass

    from app.reliability.slo import slos_from_settings

    @dataclass
    class _FakeSettings:
        slo_intent_p99_ms: float = 300.0
        slo_seek_coherent_p99_ms: float = 120.0
        slo_availability_target: float = 0.99

    slos = slos_from_settings(_FakeSettings())
    by_name = {s.name: s for s in slos.slos}
    assert by_name["intent-p99"].target == 300.0
    assert by_name["seek-p99"].target == 120.0
    assert by_name["availability"].target == 0.99


def test_slos_from_real_settings() -> None:
    # The actual Settings object exposes the additive reliability fields.
    from app.core.config import Settings
    from app.reliability.slo import slos_from_settings

    settings = Settings(dashscope_api_key="test")
    slos = slos_from_settings(settings)
    by_name = {s.name: s for s in slos.slos}
    assert by_name["intent-p99"].target == settings.slo_intent_p99_ms
    assert by_name["availability"].target == settings.slo_availability_target
