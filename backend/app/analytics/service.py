"""The analytics service façade — what the API route and rollup job call.

:class:`AnalyticsService` owns the end-to-end flows over an
:class:`~app.analytics.store.AnalyticsStore`:

* **ingest** — validate the closed taxonomy, scrub each raw event (PII-safe),
  and idempotently append. Returns an :class:`IngestResult` (accepted / new /
  rejected counts) so a client knows what landed.
* **query** — run a :class:`~app.analytics.query.Query` over the stored events.
* **sessionize / engagement / funnel / retention / cohorts** — fetch the relevant
  window from the store and run the pure analysis modules.
* **rollup** — fold a window into summary rows (the persistence of those rows is
  the repo's job; the service produces them and, when a sink is provided, writes).

The service is store-agnostic: tests pass an :class:`InMemoryAnalyticsStore`;
production passes the Postgres-backed store. The only configuration it needs is
the scrub ``salt`` and the sessionization ``gap``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.analytics.cohorts import CohortAssignment, CohortMetric, MetricFn, cohort_metrics
from app.analytics.engagement import EngagementSummary, summarize_engagement
from app.analytics.events import EventName, RawEvent
from app.analytics.funnel import FunnelResult, analyze_funnel
from app.analytics.query import Query, QueryResult, run_query
from app.analytics.retention import RetentionMatrix, retention_matrix
from app.analytics.rollup import RollupRow, compute_rollups
from app.analytics.scrub import scrub_event
from app.analytics.sessionize import DEFAULT_GAP, ReadingSession, sessionize
from app.analytics.sink import SummarySink
from app.analytics.store import AnalyticsStore
from app.analytics.timebucket import Granularity
from app.core.logging import get_logger

logger = get_logger("app.analytics.service")


@dataclass(frozen=True)
class IngestResult:
    """Outcome of an ingest batch."""

    received: int
    accepted: int
    new: int
    rejected: int
    errors: list[str] = field(default_factory=list)


class AnalyticsService:
    """Stateless façade over an :class:`AnalyticsStore` + the pure analyses."""

    def __init__(
        self,
        store: AnalyticsStore,
        *,
        salt: str,
        session_gap: timedelta = DEFAULT_GAP,
        max_batch: int = 500,
    ) -> None:
        self._store = store
        self._salt = salt
        self._gap = session_gap
        self._max_batch = max_batch

    @property
    def store(self) -> AnalyticsStore:
        """The backing store (exposed for the rollup/sessionize sinks)."""
        return self._store

    # -- ingest ------------------------------------------------------------- #

    async def ingest(self, raw_events: list[RawEvent]) -> IngestResult:
        """Scrub + idempotently store ``raw_events`` (already-validated models).

        ``RawEvent`` validation (closed taxonomy, tz-aware timestamp) happens at
        the pydantic boundary, so by the time a list arrives here every item is a
        known event. The service scrubs each into a :class:`TrackedEvent` and
        appends; the store dedupes on ``event_id``.
        """
        received = len(raw_events)
        if received > self._max_batch:
            return IngestResult(
                received=received,
                accepted=0,
                new=0,
                rejected=received,
                errors=[f"batch too large: {received} > {self._max_batch}"],
            )
        # received_at is stamped per-event by scrub_event's default clock.
        tracked = [scrub_event(raw, salt=self._salt) for raw in raw_events]
        new = await self._store.append(tracked)
        logger.info("analytics.ingest", received=received, new=new)
        return IngestResult(
            received=received,
            accepted=len(tracked),
            new=new,
            rejected=0,
        )

    # -- query -------------------------------------------------------------- #

    async def run(self, query: Query) -> QueryResult:
        """Execute a time-bucketed query over the stored events in its window."""
        events = await self._store.query(
            names=list(query.filters.names) if query.filters.names else None,
            book_id=query.filters.book_id,
            anon_user_id=query.filters.anon_user_id,
            session_key=query.filters.session_key,
            since=query.since,
            until=query.until,
        )
        return run_query(query, events)

    # -- sessionization + engagement --------------------------------------- #

    async def sessions(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        book_id: str | None = None,
    ) -> list[ReadingSession]:
        """Sessionize the events in the window (optionally scoped to a book)."""
        events = await self._store.query(since=since, until=until, book_id=book_id)
        return sessionize(events, gap=self._gap)

    async def engagement(
        self,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        book_id: str | None = None,
    ) -> EngagementSummary:
        """Population reading-engagement summary over the windowed sessions."""
        sessions = await self.sessions(since=since, until=until, book_id=book_id)
        return summarize_engagement(sessions)

    # -- funnel / retention / cohorts -------------------------------------- #

    async def funnel(
        self,
        steps: list[EventName],
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        window: timedelta | None = None,
    ) -> FunnelResult:
        """Ordered-step funnel over the windowed population."""
        events = await self._store.query(since=since, until=until)
        return analyze_funnel(events, steps, window=window)

    async def retention(
        self,
        *,
        granularity: Granularity = Granularity.DAY,
        max_offset: int = 7,
        rolling: bool = False,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> RetentionMatrix:
        """Cohort retention matrix over the windowed population."""
        events = await self._store.query(since=since, until=until)
        return retention_matrix(
            events, granularity=granularity, max_offset=max_offset, rolling=rolling
        )

    async def cohort_metric(
        self,
        assignment: CohortAssignment,
        metric: MetricFn,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[CohortMetric]:
        """Apply a metric function to each cohort's slice of the windowed events."""
        events = await self._store.query(since=since, until=until)
        return cohort_metrics(events, assignment, metric)

    # -- rollups ------------------------------------------------------------ #

    async def compute_rollups(
        self,
        *,
        since: datetime,
        until: datetime,
        granularity: Granularity = Granularity.DAY,
    ) -> list[RollupRow]:
        """Fold the windowed events into summary rows (does not persist them)."""
        events = await self._store.query(since=since, until=until)
        return compute_rollups(events, granularity=granularity)

    # -- persisted rollup / sessionize jobs (the worker drives these) -------- #

    async def run_rollup_job(
        self,
        sink: SummarySink,
        *,
        since: datetime,
        until: datetime,
        granularities: tuple[Granularity, ...] = (Granularity.DAY, Granularity.WEEK),
        persist_sessions: bool = True,
    ) -> RollupJobResult:
        """Fold the window into summary rows at each granularity and persist them.

        One pass over the windowed events feeds every granularity's rollup plus
        (optionally) the derived-session upsert. Idempotent end-to-end: the sink
        upserts on the natural key, so re-running the same window overwrites
        rather than duplicates.
        """
        events = await self._store.query(since=since, until=until)
        rollup_rows = 0
        for granularity in granularities:
            rows = compute_rollups(events, granularity=granularity)
            rollup_rows += await sink.write_rollups(rows)
        session_rows = 0
        if persist_sessions:
            session_rows = await sink.write_sessions(sessionize(events, gap=self._gap))
        logger.info(
            "analytics.rollup_job",
            events=len(events),
            rollup_rows=rollup_rows,
            session_rows=session_rows,
        )
        return RollupJobResult(
            events=len(events),
            rollup_rows=rollup_rows,
            session_rows=session_rows,
        )


@dataclass(frozen=True)
class RollupJobResult:
    """Outcome of a persisted rollup/sessionize job pass."""

    events: int
    rollup_rows: int
    session_rows: int


__all__ = ["AnalyticsService", "IngestResult", "RollupJobResult"]
