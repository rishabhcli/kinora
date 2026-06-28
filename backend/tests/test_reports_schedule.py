"""Unit tests for the scheduled-report planner (pure, no clock)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.reports.db_model import ReportKind
from app.reports.schedule import (
    DEFAULT_OPERATOR_SCHEDULE,
    Cadence,
    ScheduledReport,
    due_jobs,
    reader_digest_schedule,
)


def test_daily_due_on_new_day_only() -> None:
    job = ScheduledReport("d", ReportKind.BUDGET, Cadence.DAILY)
    d1 = datetime(2026, 6, 28, 9, 0, tzinfo=UTC)
    d1_later = datetime(2026, 6, 28, 23, 0, tzinfo=UTC)
    d2 = datetime(2026, 6, 29, 1, 0, tzinfo=UTC)
    assert job.is_due(now=d1, last_run=None) is True
    assert job.is_due(now=d1_later, last_run=d1) is False
    assert job.is_due(now=d2, last_run=d1) is True


def test_monthly_due_on_month_boundary() -> None:
    job = ScheduledReport("m", ReportKind.QUALITY, Cadence.MONTHLY)
    jun = datetime(2026, 6, 15, tzinfo=UTC)
    jun_late = datetime(2026, 6, 28, tzinfo=UTC)
    jul = datetime(2026, 7, 1, tzinfo=UTC)
    assert job.is_due(now=jun_late, last_run=jun) is False
    assert job.is_due(now=jul, last_run=jun) is True


def test_yearly_due_on_year_boundary() -> None:
    job = ScheduledReport("y", ReportKind.LIBRARY_OVERVIEW, Cadence.YEARLY)
    a = datetime(2026, 3, 1, tzinfo=UTC)
    b = datetime(2026, 12, 31, tzinfo=UTC)
    c = datetime(2027, 1, 1, tzinfo=UTC)
    assert job.is_due(now=b, last_run=a) is False
    assert job.is_due(now=c, last_run=a) is True


def test_due_jobs_filters_by_last_run() -> None:
    now = datetime(2026, 6, 28, 12, 0, tzinfo=UTC)
    # First run: everything is due.
    first = due_jobs(DEFAULT_OPERATOR_SCHEDULE, now=now)
    assert len(first) == len(DEFAULT_OPERATOR_SCHEDULE)
    # Same instant for all: nothing due.
    last = {s.name: now for s in DEFAULT_OPERATOR_SCHEDULE}
    assert due_jobs(DEFAULT_OPERATOR_SCHEDULE, now=now, last_runs=last) == []


def test_scheduled_report_to_request_marks_trigger() -> None:
    job = ScheduledReport("d", ReportKind.BUDGET, Cadence.DAILY, params={"ceiling_seconds": 999})
    req = job.to_request()
    assert req.kind is ReportKind.BUDGET
    assert req.trigger == "scheduled"
    assert req.params["ceiling_seconds"] == 999


def test_reader_digest_schedule_is_per_user_html_weekly() -> None:
    job = reader_digest_schedule("user-123")
    assert job.user_id == "user-123"
    assert job.kind is ReportKind.HIGHLIGHTS_DIGEST
    assert job.cadence is Cadence.WEEKLY
    assert "user-123" in job.name
