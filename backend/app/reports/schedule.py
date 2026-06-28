"""Scheduled report generation — a small catalogue of periodic report jobs.

On-demand generation goes straight through :class:`~app.reports.service.ReportService`.
*Scheduled* generation is a thin layer on top: a :class:`ScheduledReport`
describes a report to run on a cadence (daily / weekly / monthly / yearly), and
:func:`due_jobs` resolves which scheduled reports are due at a given instant. The
actual run reuses ``ReportService.generate`` with ``trigger="scheduled"``.

This module owns no clock and no loop — it is a pure planner (given "now", which
jobs are due) so it stays unit-testable. A caller (the API's idle-sweeper, a cron
container, or a test) drives it. Reader digests + operator dashboards are the
natural scheduled reports; certificates + year-in-review are on-demand by nature
(they fire on an event), so they're not in the default catalogue.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from app.reports.db_model import ReportKind
from app.reports.render import ReportFormat
from app.reports.service import ReportRequest


class Cadence(enum.StrEnum):
    """How often a scheduled report runs."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    YEARLY = "yearly"


@dataclass(frozen=True, slots=True)
class ScheduledReport:
    """A report to generate on a cadence (a recurring operator dashboard / digest)."""

    name: str
    kind: ReportKind
    cadence: Cadence
    fmt: ReportFormat = ReportFormat.PDF
    #: For reader digests this is templated per-user by the caller; operator
    #: dashboards leave it None (fleet-wide).
    user_id: str | None = None
    params: dict | None = None

    def is_due(self, *, now: datetime, last_run: datetime | None) -> bool:
        """Whether this schedule is due at ``now`` given its ``last_run``."""
        if last_run is None:
            return True
        return _period_key(self.cadence, now) != _period_key(self.cadence, last_run)

    def to_request(self, *, trigger: str = "scheduled") -> ReportRequest:
        """The :class:`ReportRequest` this schedule generates."""
        return ReportRequest(
            kind=self.kind,
            fmt=self.fmt,
            user_id=self.user_id,
            trigger=trigger,
            params=dict(self.params or {}),
        )


def _period_key(cadence: Cadence, when: datetime) -> tuple[int, ...]:
    """A bucket key for ``when`` at the given cadence (same key == same period)."""
    w = when.astimezone(UTC)
    if cadence is Cadence.DAILY:
        return (w.year, w.month, w.day)
    if cadence is Cadence.WEEKLY:
        iso = w.isocalendar()
        return (iso.year, iso.week)
    if cadence is Cadence.MONTHLY:
        return (w.year, w.month)
    return (w.year,)


#: The default operator dashboards that run on a cadence.
DEFAULT_OPERATOR_SCHEDULE: tuple[ScheduledReport, ...] = (
    ScheduledReport("daily-budget", ReportKind.BUDGET, Cadence.DAILY),
    ScheduledReport("daily-throughput", ReportKind.RENDER_THROUGHPUT, Cadence.DAILY),
    ScheduledReport("weekly-quality", ReportKind.QUALITY, Cadence.WEEKLY),
    ScheduledReport("weekly-library", ReportKind.LIBRARY_OVERVIEW, Cadence.WEEKLY),
)


def due_jobs(
    schedule: Sequence[ScheduledReport],
    *,
    now: datetime,
    last_runs: dict[str, datetime] | None = None,
) -> list[ScheduledReport]:
    """Return the schedules due at ``now`` given a map of ``name -> last_run``."""
    runs = last_runs or {}
    return [s for s in schedule if s.is_due(now=now, last_run=runs.get(s.name))]


def reader_digest_schedule(user_id: str) -> ScheduledReport:
    """A weekly highlights-digest schedule for one reader (templated per user)."""
    return ScheduledReport(
        name=f"weekly-digest:{user_id}",
        kind=ReportKind.HIGHLIGHTS_DIGEST,
        cadence=Cadence.WEEKLY,
        fmt=ReportFormat.HTML,
        user_id=user_id,
    )


__all__ = [
    "DEFAULT_OPERATOR_SCHEDULE",
    "Cadence",
    "ScheduledReport",
    "due_jobs",
    "reader_digest_schedule",
]
