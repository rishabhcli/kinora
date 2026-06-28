"""Report builders — turn read-only source aggregates into a :class:`Report`.

Each builder is a **pure function** of the dataclasses :mod:`app.reports.sources`
produces (plus a generation timestamp), so a test can hand it a hand-built
aggregate and assert on the resulting report tree — no database, no infra. The
service (:mod:`app.reports.service`) wires the sources to the builders; the
builders never query.

Two families:

* **reader** (:mod:`app.reports.builders.reader`) — reading-progress,
  completion certificate, year-in-review, highlights digest.
* **operator** (:mod:`app.reports.builders.operator`) — budget burn, the §13
  quality proof, render throughput, library overview.
"""

from __future__ import annotations

from app.reports.builders.operator import (
    build_budget_report,
    build_library_overview_report,
    build_quality_report,
    build_throughput_report,
)
from app.reports.builders.reader import (
    build_completion_certificate,
    build_highlights_digest,
    build_reading_progress_report,
    build_year_in_review,
)

__all__ = [
    "build_budget_report",
    "build_completion_certificate",
    "build_highlights_digest",
    "build_library_overview_report",
    "build_quality_report",
    "build_reading_progress_report",
    "build_throughput_report",
    "build_year_in_review",
]
