"""Book-scoped read/write of the portable tables (the DB side of export/import).

Export needs *all* rows of a book across every portable table; import needs to
insert remapped rows back, parent-table-first, in one unit of work. Most tables
carry a ``book_id`` column so the read is a simple filter; the two exceptions are
handled explicitly:

* ``books`` — filtered by ``id == book_id`` (it *is* the book row);
* ``render_jobs`` — has no ``book_id`` (it references the book transitively via
  ``shot_id`` / ``session_id``), so it is read by the set of the book's shot ids
  and session ids.

Reads are **streamed** with a server-side-style row iteration (``yield_per``) so
exporting a 2000-shot book never materializes the whole table in Python; the
archive writer consumes the generator lazily.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dataportability.idremap import REFERENCE_COLUMNS
from app.dataportability.serialization import RowCodec, table_registry

#: How many rows to fetch per round-trip when streaming a table.
STREAM_BATCH = 500


class BookReader:
    """Stream a book's rows out of the database, one portable table at a time."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._registry = table_registry()
        self._codecs: dict[str, RowCodec] = {}

    def _codec(self, table: str) -> RowCodec:
        if table not in self._codecs:
            self._codecs[table] = RowCodec(self._registry[table])
        return self._codecs[table]

    async def _shot_ids(self, book_id: str) -> list[str]:
        from app.db.models.shot import Shot

        rows = await self._session.execute(select(Shot.id).where(Shot.book_id == book_id))
        return [r[0] for r in rows.all()]

    async def _session_ids(self, book_id: str) -> list[str]:
        from app.db.models.session import Session

        rows = await self._session.execute(
            select(Session.id).where(Session.book_id == book_id)
        )
        return [r[0] for r in rows.all()]

    async def stream_table(self, table: str, book_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield every portable row of ``table`` for ``book_id`` as a dict."""
        model = self._registry[table]
        codec = self._codec(table)
        stmt = await self._book_filter(table, model, book_id)
        if stmt is None:
            return
        result = await self._session.stream(stmt.execution_options(yield_per=STREAM_BATCH))
        async for row in result.scalars():
            yield codec.to_dict(row)

    async def _book_filter(self, table: str, model: Any, book_id: str) -> Any:
        if table == "books":
            return select(model).where(model.id == book_id)
        if table == "render_jobs":
            shot_ids = await self._shot_ids(book_id)
            session_ids = await self._session_ids(book_id)
            if not shot_ids and not session_ids:
                return None
            clauses = []
            if shot_ids:
                clauses.append(model.shot_id.in_(shot_ids))
            if session_ids:
                clauses.append(model.session_id.in_(session_ids))
            return select(model).where(or_(*clauses))
        # Default: a plain book_id filter (ordered by PK for determinism).
        order_col = getattr(model, "id", None)
        stmt = select(model).where(model.book_id == book_id)
        if order_col is not None:
            stmt = stmt.order_by(order_col)
        return stmt

    async def count_table(self, table: str, book_id: str) -> int:
        """Count a book's rows in ``table`` (for export manifest meta / dry-runs)."""
        count = 0
        async for _ in self.stream_table(table, book_id):
            count += 1
        return count


class BookWriter:
    """Insert remapped rows back into the database (parent-table-first)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._registry = table_registry()
        self._codecs: dict[str, RowCodec] = {}

    def _codec(self, table: str) -> RowCodec:
        if table not in self._codecs:
            self._codecs[table] = RowCodec(self._registry[table])
        return self._codecs[table]

    async def insert_rows(self, table: str, rows: Sequence[dict[str, Any]]) -> int:
        """Insert a batch of already-remapped portable rows into ``table``.

        Rows are constructed through the model so column defaults and type
        coercion apply, then added to the session and flushed (surfacing any
        constraint violation immediately — import is atomic and fails closed).

        A table with a **self-referential** FK (``entities.supersedes`` points at
        another ``entities`` row) is topologically ordered first, so a row is
        always inserted after the row it references — Postgres checks self-FKs
        per row within an ``executemany`` batch, so insert order matters.
        """
        if not rows:
            return 0
        codec = self._codec(table)
        ordered = _topo_order_self_refs(table, list(rows))
        instances = [codec.build(row) for row in ordered]
        self._session.add_all(instances)
        await self._session.flush()
        return len(instances)


def _self_ref_columns(table: str) -> list[str]:
    """Columns of ``table`` that reference ``table``'s own PK space (self-FKs)."""
    spec = REFERENCE_COLUMNS.get(table, {})
    pk_space = spec.get("id")
    if pk_space is None:
        return []
    return [col for col, space in spec.items() if col != "id" and space == pk_space]


def _topo_order_self_refs(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order ``rows`` so every self-referenced parent precedes its child.

    Only rows whose referenced id is *within the batch* impose an ordering; a
    reference to a row outside the batch (already in the DB, or null) imposes
    none. A stable Kahn's algorithm preserves input order among independents.
    """
    self_cols = _self_ref_columns(table)
    if not self_cols:
        return rows
    by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        rid = row.get("id")
        if isinstance(rid, str):
            by_id[rid] = row
    indeg: dict[str, int] = dict.fromkeys(by_id, 0)
    children: dict[str, list[str]] = {rid: [] for rid in by_id}
    for row in rows:
        rid = row.get("id")
        if not isinstance(rid, str):
            continue
        for col in self_cols:
            parent = row.get(col)
            if isinstance(parent, str) and parent in by_id and parent != rid:
                children[parent].append(rid)
                indeg[rid] += 1
    # Kahn, seeded in original order for stability.
    queue = [
        rid for r in rows if isinstance((rid := r.get("id")), str) and indeg[rid] == 0
    ]
    out_ids: list[str] = []
    while queue:
        rid = queue.pop(0)
        out_ids.append(rid)
        for child in children[rid]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    if len(out_ids) != len(by_id):
        # A cycle (should be impossible for a version chain) — fall back to input
        # order rather than dropping rows; the DB FK will catch a real violation.
        return rows
    ordered = [by_id[rid] for rid in out_ids]
    # Append any rows without a string id (none for self-ref tables, but safe).
    ordered.extend(r for r in rows if not isinstance(r.get("id"), str))
    return ordered


__all__ = ["STREAM_BATCH", "BookReader", "BookWriter"]
