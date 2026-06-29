"""SQLAlchemy adapters — wire the CDC ports to the real Kinora tables.

These are the production-facing seams. They are deliberately thin and isolated
in this module so the pure CDC core (and its unit tests) never need a database:

* :class:`SqlAlchemyRowFetcher` — a :class:`~app.streaming.cdc.polling_source.RowFetcher`
  over a single ORM model, ordering by ``(updated_at, pk)`` and projecting
  ``updated_at`` to the epoch-micros cursor the polling source expects. Honours
  the project's :class:`~app.db.mixins.SoftDeleteMixin` (``deleted_at``) so the
  tombstone strategy works.
* :class:`ViewStateCheckpointStore` — persists a materialised view's Z-set into
  the ``cdc_view_state`` table and rehydrates it, so a long-running engine can
  be restored without replaying the whole change log.

Everything is constructed with a ``session_scope`` factory (an async context
manager yielding a committing session) so the same class backs both production
and the isolated integration DB. SQLAlchemy model imports are local to call
time, keeping this module import-safe in a pure-unit context.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any

from app.streaming.cdc.events import key_str
from app.streaming.cdc.polling_source import PollCursor, RowFetcher
from app.streaming.cdc.views.delta import Row, ZSet
from app.streaming.cdc.views.view import MaterializedView

SessionScope = Callable[[], AbstractAsyncContextManager[Any]]


def _to_micros(value: Any) -> int:
    """Project an ``updated_at`` value into epoch micros for the poll cursor."""
    if isinstance(value, datetime):
        return int(value.timestamp() * 1_000_000)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


class SqlAlchemyRowFetcher(RowFetcher):
    """A :class:`RowFetcher` over one ORM model for the polling source.

    Parameters
    ----------
    session_scope:
        Async context manager yielding an :class:`AsyncSession`.
    model:
        The ORM class (its ``__table__`` columns become the row dict).
    updated_at_attr / pk_attr:
        Column names for the high-water cursor and primary key.
    soft_delete_attr:
        The soft-delete timestamp column (``deleted_at``); when present a row
        with it set is surfaced (with ``deleted_at`` non-null) so the polling
        source emits a tombstone, and excluded from snapshots.
    """

    def __init__(
        self,
        session_scope: SessionScope,
        model: type[Any],
        *,
        updated_at_attr: str = "updated_at",
        pk_attr: str = "id",
        soft_delete_attr: str | None = "deleted_at",
    ) -> None:
        self._scope = session_scope
        self._model = model
        self._updated_at = updated_at_attr
        self._pk = pk_attr
        self._soft_delete = soft_delete_attr

    def _row_dict(self, obj: Any) -> dict[str, Any]:
        cols = obj.__table__.columns.keys()
        row = {c: getattr(obj, c) for c in cols}
        row["__updated_at_micros"] = _to_micros(getattr(obj, self._updated_at, None))
        return row

    def _row_pk(self, row: Mapping[str, Any]) -> str:
        # Match the polling source's compound-cursor pk encoding.
        return key_str({self._pk: row.get(self._pk)})

    async def fetch_changed(
        self, table: str, *, after: PollCursor, limit: int
    ) -> Sequence[dict[str, Any]]:
        from datetime import UTC, datetime

        from sqlalchemy import and_, or_, select

        updated_col = getattr(self._model, self._updated_at)
        pk_col = getattr(self._model, self._pk)
        # Keyset seek: updated_at > t OR (updated_at = t AND pk > last_pk). We
        # express the timestamp boundary as a real datetime so the index is used;
        # the in-Python compound filter below makes the pk tie-break exact (pk
        # comparison in SQL is on the raw column, which matches our key_str of a
        # scalar id). Rows exactly at the boundary timestamp are admitted by SQL
        # and trimmed precisely afterwards.
        boundary_dt = datetime.fromtimestamp(after.updated_at_micros / 1_000_000, tz=UTC)
        async with self._scope() as session:
            stmt = (
                select(self._model)
                .where(
                    or_(
                        updated_col > boundary_dt,
                        and_(updated_col == boundary_dt, pk_col > after.last_pk),
                    )
                )
                .order_by(updated_col.asc(), pk_col.asc())
                .limit(limit)
            )
            objs = (await session.execute(stmt)).scalars().all()
        rows = [self._row_dict(o) for o in objs]
        # Exact compound-cursor filter (micros precision the SQL boundary rounds).
        rows = [
            r
            for r in rows
            if after.after_predicate(int(r["__updated_at_micros"]), self._row_pk(r))
        ]
        rows.sort(key=lambda r: (int(r["__updated_at_micros"]), self._row_pk(r)))
        return rows[:limit]

    async def fetch_snapshot(self, table: str, *, limit: int) -> Sequence[dict[str, Any]]:
        from sqlalchemy import select

        updated_col = getattr(self._model, self._updated_at)
        pk_col = getattr(self._model, self._pk)
        async with self._scope() as session:
            stmt = select(self._model).order_by(updated_col.asc(), pk_col.asc())
            objs = (await session.execute(stmt)).scalars().all()
        rows = [self._row_dict(o) for o in objs]
        if self._soft_delete:
            rows = [r for r in rows if r.get(self._soft_delete) is None]
        return rows[:limit]


class ViewStateCheckpointStore:
    """Persist/rehydrate a materialised view's Z-set via ``cdc_view_state``.

    A periodic checkpoint lets a restart restore views without replaying the log
    from zero. The payload is the view's output row dict; the weight is the
    Z-set multiplicity (always ``>0`` for persisted rows). Rehydration loads the
    rows back into a fresh :class:`ZSet`; the view's own per-key bookkeeping is
    *not* restored (it rebuilds lazily as new events arrive), so checkpointing is
    a read-optimisation, not a correctness dependency.
    """

    def __init__(self, session_scope: SessionScope) -> None:
        self._scope = session_scope

    async def save(self, view: MaterializedView) -> int:
        """Replace the persisted state of ``view`` with its current content.

        Returns the number of rows written.
        """
        from sqlalchemy import delete

        from app.streaming.cdc.models import CdcViewStateRow

        rows = list(view.state.items())
        async with self._scope() as session:
            await session.execute(
                delete(CdcViewStateRow).where(CdcViewStateRow.view_name == view.name)
            )
            written = 0
            for row, weight in rows:
                if weight <= 0:
                    continue
                payload = row.as_dict()
                session.add(
                    CdcViewStateRow(
                        view_name=view.name,
                        row_key=key_str(payload),
                        weight=weight,
                        payload=payload,
                    )
                )
                written += 1
        return written

    async def load(self, view_name: str) -> ZSet:
        """Rehydrate a view's Z-set from its persisted rows."""
        from sqlalchemy import select

        from app.streaming.cdc.models import CdcViewStateRow

        async with self._scope() as session:
            rows = (
                (
                    await session.execute(
                        select(CdcViewStateRow).where(CdcViewStateRow.view_name == view_name)
                    )
                )
                .scalars()
                .all()
            )
        z = ZSet()
        for r in rows:
            z.add(Row(r.payload), int(r.weight))
        return z

    async def rows(self, view_name: str) -> list[Mapping[str, Any]]:
        """The persisted output rows (the read API straight from the checkpoint)."""
        z = await self.load(view_name)
        return [r.as_dict() for r in z.rows()]


def kinora_key_columns() -> dict[str, tuple[str, ...]]:
    """Primary-key columns for the Kinora tables the CDC plane mirrors.

    Used to configure the WAL decoder (``key_columns_by_table``) so update/delete
    events project the right key. Composite where the schema uses one
    (``entities`` is versioned by ``(book_id, entity_key)`` but its physical PK is
    ``id``; CDC keys on the physical PK for row identity).
    """
    return {
        "books": ("id",),
        "pages": ("id",),
        "entities": ("id",),
        "continuity_states": ("id",),
        "shots": ("id",),
        "scenes": ("id",),
        "beats": ("id",),
    }


def kinora_polled_tables() -> Iterable[str]:
    """The default set of tables the polling fallback watches."""
    return ("books", "pages", "entities", "continuity_states", "shots")


__all__ = [
    "SessionScope",
    "SqlAlchemyRowFetcher",
    "ViewStateCheckpointStore",
    "kinora_key_columns",
    "kinora_polled_tables",
]
