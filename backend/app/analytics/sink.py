"""The summary-sink seam — where rollup rows and derived sessions are persisted.

The analysis modules *produce* :class:`~app.analytics.rollup.RollupRow` and
:class:`~app.analytics.sessionize.ReadingSession` objects; a :class:`SummarySink`
*persists* them idempotently into the summary tables. Keeping this a seam (rather
than calling the repo directly from the service) lets the rollup/sessionize jobs
run over the in-memory store in tests while the real job writes to Postgres.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol, runtime_checkable

from app.analytics.rollup import RollupRow
from app.analytics.sessionize import ReadingSession


@runtime_checkable
class SummarySink(Protocol):
    """Idempotent persistence for rollup rows and derived sessions."""

    async def write_rollups(self, rows: Iterable[RollupRow]) -> int:
        """Upsert summary rows on their grain; return the row count."""
        ...

    async def write_sessions(self, sessions: Iterable[ReadingSession]) -> int:
        """Upsert derived sessions on ``session_id``; return the row count."""
        ...


class InMemorySummarySink:
    """A deterministic in-memory sink (tests). Last write per key wins."""

    def __init__(self) -> None:
        self.rollups: dict[tuple[str, str, str, str], RollupRow] = {}
        self.sessions: dict[str, ReadingSession] = {}

    async def write_rollups(self, rows: Iterable[RollupRow]) -> int:
        count = 0
        for row in rows:
            self.rollups[row.key] = row
            count += 1
        return count

    async def write_sessions(self, sessions: Iterable[ReadingSession]) -> int:
        count = 0
        for s in sessions:
            self.sessions[s.session_id] = s
            count += 1
        return count


__all__ = ["InMemorySummarySink", "SummarySink"]
