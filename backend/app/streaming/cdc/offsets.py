"""Resumable offsets — where a source restarts after a crash.

A source advances through a change log; on restart it must resume *after* the
last position it durably handed off, never replaying acknowledged events twice
(or, for at-least-once, replaying only a bounded tail). An :class:`OffsetStore`
records, per ``(connector, table)``, the highest :class:`LogPosition` that has
been committed downstream.

Two implementations:

* :class:`InMemoryOffsetStore` — deterministic tests / single process.
* :class:`DbOffsetStore` — persists to the ``cdc_offsets`` table (see
  ``models.py`` / migration ``cdc_0001``) so a restart resumes across processes.

The DB store is written against the project's async repository pattern but is
optional: when no session factory is supplied the pipeline falls back to the
in-memory store, so the unit suite runs with zero infra.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol, runtime_checkable

from app.streaming.cdc.events import LogPosition


@runtime_checkable
class OffsetStore(Protocol):
    """Durable record of the committed position per connector/table."""

    async def load(self, connector: str, table: str) -> LogPosition:
        """Return the committed position, or :meth:`LogPosition.zero` if none."""
        ...

    async def commit(self, connector: str, table: str, position: LogPosition) -> None:
        """Persist ``position`` as committed (monotonic; never moves backwards)."""
        ...


class InMemoryOffsetStore:
    """A dict-backed offset store for tests and single-process runs."""

    def __init__(self) -> None:
        self._offsets: dict[tuple[str, str], LogPosition] = {}

    async def load(self, connector: str, table: str) -> LogPosition:
        return self._offsets.get((connector, table), LogPosition.zero())

    async def commit(self, connector: str, table: str, position: LogPosition) -> None:
        key = (connector, table)
        current = self._offsets.get(key, LogPosition.zero())
        # Monotonic guard: a late commit from a slower consumer can't rewind.
        if position >= current:
            self._offsets[key] = position

    # -- introspection (debug / metrics) ----------------------------------- #
    def snapshot(self) -> dict[tuple[str, str], LogPosition]:
        return dict(self._offsets)


class DbOffsetStore:
    """Persists offsets to the ``cdc_offsets`` table across process restarts.

    Constructed with a ``session_scope`` factory — an async context manager that
    yields a committing :class:`~sqlalchemy.ext.asyncio.AsyncSession` (the
    project's :func:`app.db.session.get_session` unit-of-work boundary). This
    keeps the store free of any module-level engine and lets the same class back
    both production (real engine) and the integration tests (isolated DB).

    The SQLAlchemy model import is deferred to call time so importing this module
    never forces ``app.db`` to be importable in a pure-unit context.
    """

    def __init__(
        self,
        session_scope: Callable[[], AbstractAsyncContextManager[Any]],
    ) -> None:
        self._session_scope = session_scope

    async def load(self, connector: str, table: str) -> LogPosition:
        from sqlalchemy import select

        from app.streaming.cdc.models import CdcOffset

        async with self._session_scope() as session:
            row = (
                await session.execute(
                    select(CdcOffset).where(
                        CdcOffset.connector == connector,
                        CdcOffset.table_name == table,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return LogPosition.zero()
            return LogPosition(int(row.position_major), int(row.position_minor))

    async def commit(self, connector: str, table: str, position: LogPosition) -> None:
        from sqlalchemy import select

        from app.streaming.cdc.models import CdcOffset

        async with self._session_scope() as session:
            row = (
                await session.execute(
                    select(CdcOffset).where(
                        CdcOffset.connector == connector,
                        CdcOffset.table_name == table,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                session.add(
                    CdcOffset(
                        connector=connector,
                        table_name=table,
                        position_major=position.major,
                        position_minor=position.minor,
                    )
                )
                return
            current = LogPosition(int(row.position_major), int(row.position_minor))
            if position >= current:  # monotonic guard
                row.position_major = position.major
                row.position_minor = position.minor


__all__ = ["DbOffsetStore", "InMemoryOffsetStore", "OffsetStore"]
