"""Unit tests for the ConformanceReport model: verdict, score, summary."""

from __future__ import annotations

from datetime import UTC, datetime

from app.video.conformance.report import (
    CheckOutcome,
    CheckResult,
    ConformanceCheck,
    ConformanceReport,
)


def _result(check: ConformanceCheck, outcome: CheckOutcome) -> CheckResult:
    return CheckResult(check=check, outcome=outcome)


def test_passed_true_when_only_pass_and_skip() -> None:
    report = ConformanceReport(
        provider_id="p",
        results=[
            _result(ConformanceCheck.SURFACE, CheckOutcome.PASS),
            _result(ConformanceCheck.CANCELLATION, CheckOutcome.SKIP),
        ],
    )
    assert report.passed
    assert report.failures == []


def test_passed_false_on_fail_or_error() -> None:
    for bad in (CheckOutcome.FAIL, CheckOutcome.ERROR):
        report = ConformanceReport(
            provider_id="p",
            results=[
                _result(ConformanceCheck.SURFACE, CheckOutcome.PASS),
                _result(ConformanceCheck.TIMEOUT, bad),
            ],
        )
        assert not report.passed
        assert [r.check for r in report.failures] == [ConformanceCheck.TIMEOUT]


def test_score_ignores_skips() -> None:
    report = ConformanceReport(
        provider_id="p",
        results=[
            _result(ConformanceCheck.SURFACE, CheckOutcome.PASS),
            _result(ConformanceCheck.TIMEOUT, CheckOutcome.FAIL),
            _result(ConformanceCheck.CANCELLATION, CheckOutcome.SKIP),
        ],
    )
    # 1 of 2 executed (skip excluded) → 0.5.
    assert report.score == 0.5
    assert len(report.executed) == 2


def test_score_is_one_when_nothing_executed() -> None:
    report = ConformanceReport(
        provider_id="p",
        results=[_result(ConformanceCheck.SURFACE, CheckOutcome.SKIP)],
    )
    assert report.score == 1.0
    assert report.passed


def test_result_for_returns_first_match_or_none() -> None:
    report = ConformanceReport(
        provider_id="p",
        results=[_result(ConformanceCheck.SURFACE, CheckOutcome.PASS)],
    )
    assert report.result_for(ConformanceCheck.SURFACE) is not None
    assert report.result_for(ConformanceCheck.TIMEOUT) is None


def test_summary_and_render_text_reflect_verdict() -> None:
    report = ConformanceReport(
        provider_id="acme-video",
        results=[
            _result(ConformanceCheck.SURFACE, CheckOutcome.PASS),
            _result(ConformanceCheck.TIMEOUT, CheckOutcome.FAIL),
        ],
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    summary = report.summary()
    assert "FAIL" in summary
    assert "acme-video" in summary
    text = report.render_text()
    assert "surface" in text
    assert "timeout" in text


def test_check_result_ok_property() -> None:
    assert _result(ConformanceCheck.SURFACE, CheckOutcome.PASS).ok
    assert _result(ConformanceCheck.SURFACE, CheckOutcome.SKIP).ok
    assert not _result(ConformanceCheck.SURFACE, CheckOutcome.FAIL).ok
    assert not _result(ConformanceCheck.SURFACE, CheckOutcome.ERROR).ok


def test_report_is_frozen() -> None:
    report = ConformanceReport(provider_id="p", results=[])
    import pydantic

    try:
        report.provider_id = "q"  # type: ignore[misc]
    except (pydantic.ValidationError, AttributeError, TypeError):
        return
    raise AssertionError("expected the frozen report to reject mutation")
