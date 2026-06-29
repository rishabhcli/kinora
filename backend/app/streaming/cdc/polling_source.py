"""Polling CDC fallback — updated-at high-watermark + tombstone strategy.

When logical replication isn't available (managed Postgres without a replication
slot, a read replica, or a table without a publication) we fall back to polling.
The strategy is the standard "incremental query CDC":

* Every table carries ``updated_at`` (the project's :class:`TimestampMixin`
  already provides it) and an immutable primary key.
* Each poll asks for rows with ``updated_at > high_water`` ordered by
  ``(updated_at, pk)`` and turns each into an INSERT (pk unseen) or UPDATE (pk
  seen) event. The high-water mark advances to the last row's
  ``(updated_at, pk)``, encoded into a :class:`LogPosition` so resume is exact.
* **Deletes can't be observed by an updated-at query** (the row is gone), so the
  fallback needs a **tombstone strategy**: instead of hard-deleting, callers
  soft-delete (the project's :class:`SoftDeleteMixin` sets ``deleted_at``); the
  poller emits a DELETE for any row whose ``deleted_at`` is newly set, then the
  tombstone can be reaped after the poller has passed it.

This module is written against a small :class:`RowFetcher` port so it is
testable with zero infra: a fake fetcher returns scripted snapshots/changed
rows. A DB-backed fetcher (SQLAlchemy) is a thin adapter the pipeline can inject
in production; it is intentionally *not* imported here so the unit suite needs
no database.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from app.streaming.cdc.clock import Clock, SystemClock
from app.streaming.cdc.events import ChangeEvent, JsonRow, LogPosition, Op, key_str
from app.streaming.cdc.source import CDCSource


@dataclass(slots=True)
class PollCursor:
    """The high-water mark for incremental polling.

    A pure ``updated_at`` cursor is **not** safe across a batch boundary: if more
    rows than ``batch_size`` share one ``updated_at`` value, advancing the cursor
    to that value and then filtering strictly ``>`` skips the rest. The standard
    fix is a **compound cursor** ``(updated_at, pk)`` with the resume predicate
    ``updated_at > t OR (updated_at = t AND pk > last_pk)`` and a matching
    ``ORDER BY updated_at, pk``. We therefore carry the last emitted row's primary
    key alongside the timestamp.

    A synthetic monotonic ``pk_tiebreak`` is still kept for encoding into a
    totally-ordered :class:`LogPosition` (the change-event position), independent
    of the resume cursor.
    """

    updated_at_micros: int = 0
    last_pk: str = ""
    pk_tiebreak: int = 0

    def to_position(self) -> LogPosition:
        return LogPosition(self.updated_at_micros, self.pk_tiebreak)

    @classmethod
    def from_position(cls, pos: LogPosition) -> PollCursor:
        return cls(updated_at_micros=pos.major, pk_tiebreak=pos.minor)

    def after_predicate(self, updated_micros: int, pk: str) -> bool:
        """Whether a row at ``(updated_micros, pk)`` is strictly after this cursor."""
        if updated_micros != self.updated_at_micros:
            return updated_micros > self.updated_at_micros
        return pk > self.last_pk


class RowFetcher(abc.ABC):
    """Port the poller reads rows through (DB adapter or fake)."""

    @abc.abstractmethod
    async def fetch_changed(
        self, table: str, *, after: PollCursor, limit: int
    ) -> Sequence[JsonRow]:
        """Rows strictly after ``after`` by ``(updated_at, pk)``, ordered + limited.

        Each row must include the table's primary key, ``updated_at`` (as epoch
        micros under key ``__updated_at_micros``), and — for the tombstone
        strategy — ``deleted_at`` (``None`` when live).
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def fetch_snapshot(self, table: str, *, limit: int) -> Sequence[JsonRow]:
        """All currently-live rows, ordered by ``(updated_at, pk)``."""
        raise NotImplementedError


@dataclass(slots=True)
class _TableState:
    cursor: PollCursor = field(default_factory=PollCursor)
    seen_keys: set[str] = field(default_factory=set)
    tombstoned: set[str] = field(default_factory=set)


class PollingSource(CDCSource):
    """CDC by repeatedly diffing ``updated_at``; soft-delete tombstones for deletes.

    One instance covers a set of tables. ``poll_once`` does a single pass over
    all tables and returns the events it produced (the pipeline decides cadence,
    so tests stay timer-free). :meth:`stream` drives ``poll_once`` until the
    source is exhausted of *currently available* changes — combined with a
    :class:`~app.streaming.cdc.clock.FakeClock` this is fully deterministic.
    """

    def __init__(
        self,
        fetcher: RowFetcher,
        tables: Sequence[str],
        *,
        key_columns: tuple[str, ...] = ("id",),
        batch_size: int = 256,
        clock: Clock | None = None,
    ) -> None:
        self._fetcher = fetcher
        self._tables = list(tables)
        self._key_columns = key_columns
        self._batch_size = batch_size
        self._clock = clock or SystemClock()
        self._state: dict[str, _TableState] = {t: _TableState() for t in tables}

    # -- single poll -------------------------------------------------------- #
    async def poll_once(self) -> list[ChangeEvent]:
        """One pass over every table; emit insert/update/delete events."""
        events: list[ChangeEvent] = []
        for table in self._tables:
            events.extend(await self._poll_table(table))
        return events

    async def _poll_table(self, table: str) -> list[ChangeEvent]:
        state = self._state[table]
        out: list[ChangeEvent] = []
        while True:
            rows = await self._fetcher.fetch_changed(
                table, after=state.cursor, limit=self._batch_size
            )
            if not rows:
                break
            for row in rows:
                out.append(self._row_to_event(table, row, state))
            if len(rows) < self._batch_size:
                break
        return out

    def _row_to_event(self, table: str, row: JsonRow, state: _TableState) -> ChangeEvent:
        updated_micros = int(row.get("__updated_at_micros", 0))
        body = {k: v for k, v in row.items() if k != "__updated_at_micros"}
        k = key_str({c: body.get(c) for c in self._key_columns})
        # The resume key within a timestamp is the row's primary key (compound
        # cursor); a stable string for single- or multi-column keys.
        pk = key_str({c: body.get(c) for c in self._key_columns})

        # Advance the compound cursor (updated_at, pk); the synthetic tiebreak
        # only feeds the totally-ordered LogPosition, not the resume predicate.
        new_cursor = PollCursor(
            updated_at_micros=updated_micros,
            last_pk=pk,
            pk_tiebreak=state.cursor.pk_tiebreak + 1,
        )
        position = new_cursor.to_position()
        state.cursor = new_cursor

        deleted = body.get("deleted_at") is not None
        if deleted and k not in state.tombstoned:
            state.tombstoned.add(k)
            return ChangeEvent.delete(
                table,
                body,
                position,
                key_columns=self._key_columns,
                ts=self._clock.time(),
                meta={"via": "polling.tombstone"},
            )
        if k in state.seen_keys:
            return ChangeEvent.update(
                table,
                None,  # polling can't cheaply produce a before-image
                body,
                position,
                key_columns=self._key_columns,
                ts=self._clock.time(),
                meta={"via": "polling"},
            )
        state.seen_keys.add(k)
        return ChangeEvent.insert(
            table,
            body,
            position,
            key_columns=self._key_columns,
            ts=self._clock.time(),
            meta={"via": "polling"},
        )

    # -- CDCSource interface ----------------------------------------------- #
    async def snapshot(self) -> AsyncIterator[ChangeEvent]:
        minor = 0
        for table in self._tables:
            rows = await self._fetcher.fetch_snapshot(table, limit=10**9)
            for row in rows:
                body = {k: v for k, v in row.items() if k != "__updated_at_micros"}
                key = key_str({c: body.get(c) for c in self._key_columns})
                self._state[table].seen_keys.add(key)
                # Track the compound cursor so streaming resumes strictly after
                # the last snapshot row by (updated_at, pk).
                um = int(row.get("__updated_at_micros", 0))
                st = self._state[table]
                if st.cursor.after_predicate(um, key):
                    st.cursor = PollCursor(updated_at_micros=um, last_pk=key)
                yield ChangeEvent.read(
                    table,
                    body,
                    LogPosition(0, minor),
                    key_columns=self._key_columns,
                    ts=self._clock.time(),
                )
                minor += 1

    async def stream(self, *, after: LogPosition | None = None) -> AsyncIterator[ChangeEvent]:
        if after is not None:
            cursor = PollCursor.from_position(after)
            for st in self._state.values():
                if cursor.updated_at_micros > st.cursor.updated_at_micros:
                    st.cursor = cursor
        for ev in await self.poll_once():
            yield ev

    def cursor(self, table: str) -> PollCursor:
        return self._state[table].cursor


class ListRowFetcher(RowFetcher):
    """A deterministic in-memory :class:`RowFetcher` for tests.

    Seed per-table row lists; ``fetch_changed`` returns rows strictly after the
    *compound* cursor ``(updated_at, pk)`` ordered by the same, so a batch
    boundary that splits rows sharing one ``updated_at`` resumes correctly.
    Mutating a row's ``__updated_at_micros`` (and re-adding it) simulates an
    UPDATE; setting ``deleted_at`` simulates a soft delete. Keyed on ``id``.
    """

    def __init__(self) -> None:
        self._rows: dict[str, list[JsonRow]] = {}

    def set_rows(self, table: str, rows: Sequence[JsonRow]) -> None:
        self._rows[table] = [dict(r) for r in rows]

    def upsert(self, table: str, row: JsonRow) -> None:
        self._rows.setdefault(table, []).append(dict(row))

    @staticmethod
    def _pk(row: JsonRow) -> str:
        # Match the compound-cursor pk encoding (key_str over the "id" column).
        return key_str({"id": row.get("id")})

    async def fetch_changed(
        self, table: str, *, after: PollCursor, limit: int
    ) -> Sequence[JsonRow]:
        rows = self._rows.get(table, [])
        changed = [
            r
            for r in rows
            if after.after_predicate(int(r.get("__updated_at_micros", 0)), self._pk(r))
        ]
        changed.sort(key=lambda r: (int(r.get("__updated_at_micros", 0)), self._pk(r)))
        return changed[:limit]

    async def fetch_snapshot(self, table: str, *, limit: int) -> Sequence[JsonRow]:
        rows = [r for r in self._rows.get(table, []) if r.get("deleted_at") is None]
        rows.sort(key=lambda r: (int(r.get("__updated_at_micros", 0)), str(r.get("id"))))
        return rows[:limit]


__all__ = [
    "ListRowFetcher",
    "PollCursor",
    "PollingSource",
    "RowFetcher",
    "Op",
]
