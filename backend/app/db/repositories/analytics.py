"""Repository for the product-analytics tables (idempotent persistence).

* :meth:`AnalyticsRepo.append_events` inserts scrubbed events with
  ``ON CONFLICT (event_id) DO NOTHING`` so a retried ingest batch is a no-op and
  the method returns how many rows were *newly* inserted.
* :meth:`AnalyticsRepo.query_events` is the filtered, ordered read every analysis
  runs over (mirrors :func:`app.analytics.store.matches`).
* :meth:`AnalyticsRepo.upsert_sessions` / :meth:`upsert_rollups` overwrite their
  rows on the natural key so recomputation is idempotent.

The repo *flushes* (never commits) — the unit-of-work boundary owns the
transaction (see :class:`app.db.repositories.base.BaseRepository`).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.analytics.events import EventName, ReadMode, TrackedEvent
from app.analytics.rollup import RollupRow
from app.analytics.sessionize import ReadingSession
from app.db.base import new_id
from app.db.models.analytics import AnalyticsDailyRollup, AnalyticsEvent, AnalyticsSession
from app.db.repositories.base import BaseRepository


def _to_tracked(row: AnalyticsEvent) -> TrackedEvent:
    """Map an ORM row back to the canonical :class:`TrackedEvent`."""
    return TrackedEvent(
        event_id=row.event_id,
        name=EventName(row.name),
        occurred_at=row.occurred_at,
        received_at=row.received_at,
        anon_user_id=row.anon_user_id,
        book_id=row.book_id,
        session_key=row.session_key,
        mode=ReadMode(row.mode) if row.mode else None,
        props=row.props or {},
    )


class AnalyticsRepo(BaseRepository):
    """Persist + read analytics events, sessions, and rollups."""

    # -- events ------------------------------------------------------------- #

    async def append_events(self, events: Iterable[TrackedEvent]) -> int:
        """Idempotently insert ``events`` (dedupe on ``event_id``); return new count.

        Uses a single ``INSERT ... ON CONFLICT DO NOTHING`` per batch and reports
        the number of rows actually inserted (``rowcount``). Within-batch
        duplicate ``event_id``\\ s are collapsed first so the statement itself is
        well-formed.
        """
        seen: set[str] = set()
        values: list[dict[str, object]] = []
        for event in events:
            if event.event_id in seen:
                continue
            seen.add(event.event_id)
            values.append(
                {
                    "id": new_id(),
                    "event_id": event.event_id,
                    "name": event.name.value,
                    "occurred_at": event.occurred_at,
                    "received_at": event.received_at,
                    "anon_user_id": event.anon_user_id,
                    "book_id": event.book_id,
                    "session_key": event.session_key,
                    "mode": event.mode.value if event.mode else None,
                    "props": event.props or None,
                }
            )
        if not values:
            return 0
        insert_stmt = pg_insert(AnalyticsEvent).values(values)
        insert_stmt = insert_stmt.on_conflict_do_nothing(index_elements=["event_id"])
        result = cast("CursorResult[Any]", await self.session.execute(insert_stmt))
        await self.session.flush()
        # ``rowcount`` reflects the rows actually inserted (conflicts skipped).
        return int(result.rowcount or 0)

    async def query_events(
        self,
        *,
        names: Sequence[EventName] | None = None,
        book_id: str | None = None,
        anon_user_id: str | None = None,
        session_key: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int | None = None,
    ) -> list[TrackedEvent]:
        """Return matching events ordered by ``(occurred_at, event_id)``."""
        stmt = select(AnalyticsEvent)
        if names is not None:
            stmt = stmt.where(AnalyticsEvent.name.in_([n.value for n in names]))
        if book_id is not None:
            stmt = stmt.where(AnalyticsEvent.book_id == book_id)
        if anon_user_id is not None:
            stmt = stmt.where(AnalyticsEvent.anon_user_id == anon_user_id)
        if session_key is not None:
            stmt = stmt.where(AnalyticsEvent.session_key == session_key)
        if since is not None:
            stmt = stmt.where(AnalyticsEvent.occurred_at >= since)
        if until is not None:
            stmt = stmt.where(AnalyticsEvent.occurred_at < until)
        stmt = stmt.order_by(AnalyticsEvent.occurred_at, AnalyticsEvent.event_id)
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_to_tracked(r) for r in rows]

    async def count_events(self) -> int:
        """Total stored events."""
        stmt = select(func.count()).select_from(AnalyticsEvent)
        return int((await self.session.execute(stmt)).scalar_one())

    # -- sessions ----------------------------------------------------------- #

    async def upsert_sessions(self, sessions: Iterable[ReadingSession]) -> int:
        """Insert-or-update derived sessions on ``session_id``; return row count."""
        values: list[dict[str, object]] = []
        for s in sessions:
            values.append(
                {
                    "id": new_id(),
                    "session_id": s.session_id,
                    "anon_user_id": s.anon_user_id,
                    "book_id": s.book_id,
                    "started_at": s.started_at,
                    "ended_at": s.ended_at,
                    "duration_s": s.duration_s,
                    "event_count": s.event_count,
                    "pages_seen": s.pages_seen,
                    "deepest_page": s.deepest_page,
                    "words_read": s.words_read,
                    "completion_ratio": s.completion_ratio,
                    "dropoff_page": s.dropoff_page,
                    "director_event_count": s.director_event_count,
                    "stall_count": s.stall_count,
                }
            )
        if not values:
            return 0
        stmt = pg_insert(AnalyticsSession).values(values)
        update_cols = {
            c: stmt.excluded[c]
            for c in (
                "anon_user_id",
                "book_id",
                "started_at",
                "ended_at",
                "duration_s",
                "event_count",
                "pages_seen",
                "deepest_page",
                "words_read",
                "completion_ratio",
                "dropoff_page",
                "director_event_count",
                "stall_count",
            )
        }
        stmt = stmt.on_conflict_do_update(index_elements=["session_id"], set_=update_cols)
        await self.session.execute(stmt)
        await self.session.flush()
        return len(values)

    # -- rollups ------------------------------------------------------------ #

    async def upsert_rollups(self, rows: Iterable[RollupRow]) -> int:
        """Insert-or-update rollup rows on the grain tuple; return row count."""
        values: list[dict[str, object]] = []
        for r in rows:
            values.append(
                {
                    "id": new_id(),
                    "bucket_start": r.bucket_start,
                    "granularity": r.granularity.value,
                    "bucket_label": r.bucket_label,
                    "dimension_key": r.dimension_key,
                    "metric": r.metric,
                    "value": r.value,
                }
            )
        if not values:
            return 0
        stmt = pg_insert(AnalyticsDailyRollup).values(values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["bucket_start", "granularity", "dimension_key", "metric"],
            set_={"value": stmt.excluded["value"], "bucket_label": stmt.excluded["bucket_label"]},
        )
        await self.session.execute(stmt)
        await self.session.flush()
        return len(values)

    async def read_rollups(
        self,
        *,
        metric: str | None = None,
        granularity: str | None = None,
        dimension_key: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AnalyticsDailyRollup]:
        """Read summary rows, ordered by bucket then metric (for the query API)."""
        stmt = select(AnalyticsDailyRollup)
        if metric is not None:
            stmt = stmt.where(AnalyticsDailyRollup.metric == metric)
        if granularity is not None:
            stmt = stmt.where(AnalyticsDailyRollup.granularity == granularity)
        if dimension_key is not None:
            stmt = stmt.where(AnalyticsDailyRollup.dimension_key == dimension_key)
        if since is not None:
            stmt = stmt.where(AnalyticsDailyRollup.bucket_start >= since)
        if until is not None:
            stmt = stmt.where(AnalyticsDailyRollup.bucket_start < until)
        stmt = stmt.order_by(
            AnalyticsDailyRollup.bucket_start,
            AnalyticsDailyRollup.metric,
            AnalyticsDailyRollup.dimension_key,
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = ["AnalyticsRepo"]
