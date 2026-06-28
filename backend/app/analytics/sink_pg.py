"""Postgres-backed :class:`~app.analytics.sink.SummarySink`.

Wraps :class:`app.db.repositories.analytics.AnalyticsRepo` so the rollup /
sessionize jobs persist into ``analytics_daily_rollup`` / ``analytics_sessions``
through a committing unit of work, idempotently (the repo upserts on the natural
key).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.analytics.rollup import RollupRow
from app.analytics.sessionize import ReadingSession
from app.composition import SessionFactory
from app.db.repositories.analytics import AnalyticsRepo


class PostgresSummarySink:
    """A :class:`SummarySink` backed by the analytics repo."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    async def write_rollups(self, rows: Iterable[RollupRow]) -> int:
        async with self._session_factory() as db:
            return await AnalyticsRepo(db).upsert_rollups(rows)

    async def write_sessions(self, sessions: Iterable[ReadingSession]) -> int:
        async with self._session_factory() as db:
            return await AnalyticsRepo(db).upsert_sessions(sessions)


__all__ = ["PostgresSummarySink"]
