"""Bulk-load helpers: chunked inserts and Postgres ``ON CONFLICT`` upserts.

Phase A ingest writes thousands of rows at once — pages, the source-span index,
the shot list (§9.1). Adding them one ``session.add`` at a time is fine for a
handful but wasteful at scale, and a single giant ``INSERT`` can blow past
Postgres' 65 535 bind-parameter ceiling. These helpers split a large row list
into bounded batches and run one multi-row statement per batch.

* :func:`bulk_insert` — chunked ``INSERT`` of plain dict rows (Core, no ORM
  identity-map cost). Returns the number of rows inserted.
* :func:`bulk_upsert` — chunked ``INSERT ... ON CONFLICT DO UPDATE``/``DO
  NOTHING`` keyed by a conflict target. This is the set-based form of the §8.7
  cache upsert / a re-ingest that must be idempotent on book-scoped ids.
* :func:`bulk_insert_returning` — chunked insert that returns a chosen column
  (e.g. generated ids) per row.

All compute the per-batch size from the column count so the parameter ceiling is
respected automatically; an explicit ``chunk_rows`` overrides it. They *flush*
(or execute) but never *commit* — the unit of work owns the transaction.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import CursorResult, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.query import chunked

# Stay comfortably below Postgres' 65535 bind-parameter cap so other params in
# the same statement (rare for bulk inserts, but possible) never tip it over.
_PARAM_CEILING = 60000


def _batch_rows(rows: Sequence[dict[str, Any]], chunk_rows: int | None) -> int:
    """Resolve the per-batch row count from the widest row's column count."""
    if chunk_rows is not None:
        if chunk_rows <= 0:
            raise ValueError("chunk_rows must be > 0")
        return chunk_rows
    max_cols = max((len(r) for r in rows), default=1)
    return max(1, _PARAM_CEILING // max(1, max_cols))


async def bulk_insert(
    session: AsyncSession,
    model: type[Any],
    rows: Sequence[dict[str, Any]],
    *,
    chunk_rows: int | None = None,
) -> int:
    """Insert ``rows`` into ``model``'s table in bounded batches; rows inserted.

    Uses a Core multi-values ``INSERT`` per batch (no ORM identity map), so a
    50k-row span index loads in a handful of round-trips. Returns the total row
    count (the sum of per-batch ``rowcount``, falling back to the input length
    when a driver doesn't report it).
    """
    if not rows:
        return 0
    size = _batch_rows(rows, chunk_rows)
    total = 0
    for batch in chunked(list(rows), size):
        # A multi-row executemany insert returns an IteratorResult without a
        # reliable rowcount across drivers, so count the input batch directly —
        # an INSERT either inserts every row in the batch or raises.
        await session.execute(insert(model), batch)
        total += len(batch)
    await session.flush()
    return total


async def bulk_upsert(
    session: AsyncSession,
    model: type[Any],
    rows: Sequence[dict[str, Any]],
    *,
    conflict_columns: Sequence[str],
    update_columns: Sequence[str] | None = None,
    chunk_rows: int | None = None,
) -> int:
    """Chunked ``INSERT ... ON CONFLICT`` upsert keyed by ``conflict_columns``.

    ``update_columns`` lists the columns to overwrite on a conflict; when empty
    or ``None`` the conflict is a ``DO NOTHING`` (insert-or-ignore). When given,
    a conflict updates exactly those columns from the proposed (``EXCLUDED``)
    row. Returns the number of rows the statements *affected* (inserts plus
    updates; ``DO NOTHING`` conflicts don't count).
    """
    if not rows:
        return 0
    if not conflict_columns:
        raise ValueError("conflict_columns is required for an upsert")
    size = _batch_rows(rows, chunk_rows)
    total = 0
    for batch in chunked(list(rows), size):
        stmt = pg_insert(model).values(batch)
        if update_columns:
            set_ = {col: getattr(stmt.excluded, col) for col in update_columns}
            stmt = stmt.on_conflict_do_update(index_elements=list(conflict_columns), set_=set_)
        else:
            stmt = stmt.on_conflict_do_nothing(index_elements=list(conflict_columns))
        result = cast("CursorResult[Any]", await session.execute(stmt))
        total += int(result.rowcount or 0)
    await session.flush()
    return total


async def bulk_insert_returning(
    session: AsyncSession,
    model: type[Any],
    rows: Sequence[dict[str, Any]],
    *,
    returning: str,
    chunk_rows: int | None = None,
) -> list[Any]:
    """Insert ``rows`` and return one column's value per inserted row.

    Handy when ids are server-generated and the caller needs them back (e.g. to
    wire up dependent rows in the same transaction). Order within a batch matches
    insertion order; across batches it is concatenated batch-by-batch.
    """
    if not rows:
        return []
    column = getattr(model, returning)
    size = _batch_rows(rows, chunk_rows)
    collected: list[Any] = []
    for batch in chunked(list(rows), size):
        stmt = insert(model).returning(column)
        result = await session.execute(stmt, batch)
        collected.extend(result.scalars().all())
    await session.flush()
    return collected


__all__ = [
    "bulk_insert",
    "bulk_insert_returning",
    "bulk_upsert",
]
