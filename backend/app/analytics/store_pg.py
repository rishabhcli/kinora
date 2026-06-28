"""Postgres-backed :class:`~app.analytics.store.AnalyticsStore`.

Wraps :class:`app.db.repositories.analytics.AnalyticsRepo` behind the store
protocol so the service layer is agnostic to whether it is talking to the
in-memory store (tests) or Postgres (production). Each operation runs in its own
committing unit of work via the injected session factory, matching the rest of
the composition root.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from app.analytics.events import EventName, TrackedEvent
from app.composition import SessionFactory
from app.db.repositories.analytics import AnalyticsRepo


class PostgresAnalyticsStore:
    """An :class:`AnalyticsStore` backed by ``analytics_events`` via the repo."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def append(self, events: Iterable[TrackedEvent]) -> int:
        async with self._session_factory() as db:
            return await AnalyticsRepo(db).append_events(events)

    async def query(
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
        async with self._session_factory() as db:
            return await AnalyticsRepo(db).query_events(
                names=names,
                book_id=book_id,
                anon_user_id=anon_user_id,
                session_key=session_key,
                since=since,
                until=until,
                limit=limit,
            )

    async def count(self) -> int:
        async with self._session_factory() as db:
            return await AnalyticsRepo(db).count_events()


__all__ = ["PostgresAnalyticsStore"]
