"""Product analytics & event pipeline (see ``DESIGN.md`` in this package).

A typed event-tracking pipeline distinct from ops-observability
(:mod:`app.observability`) and the §13 quality/eval warehouse (:mod:`app.eval`):
batched/idempotent ingestion, PII-safe scrubbing, sessionization, funnel /
retention / cohort analysis, reading-engagement metrics, time-bucketed rollups
into summary tables, and a flexible query API.

The pure analysis modules (``events``, ``scrub``, ``sessionize``, ``engagement``,
``funnel``, ``retention``, ``cohorts``, ``query``, ``timebucket``, ``rollup``)
import no infrastructure and are safe to import anywhere. The persistence seam is
:class:`app.analytics.store.AnalyticsStore`; the in-memory implementation is the
default for tests and a viable embedded backend.
"""

from __future__ import annotations

from app.analytics.events import (
    READING_EVENTS,
    EventBatch,
    EventName,
    RawEvent,
    ReadMode,
    TrackedEvent,
)
from app.analytics.service import AnalyticsService, IngestResult, RollupJobResult
from app.analytics.sink import InMemorySummarySink, SummarySink
from app.analytics.store import AnalyticsStore, InMemoryAnalyticsStore

__all__ = [
    "READING_EVENTS",
    "AnalyticsService",
    "AnalyticsStore",
    "EventBatch",
    "EventName",
    "IngestResult",
    "InMemoryAnalyticsStore",
    "InMemorySummarySink",
    "RawEvent",
    "ReadMode",
    "RollupJobResult",
    "SummarySink",
    "TrackedEvent",
]
