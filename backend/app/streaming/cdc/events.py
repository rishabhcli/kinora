"""Typed change events — the wire contract of the CDC plane.

A :class:`ChangeEvent` is the single shape every source emits and every
consumer (the view engine, an external sink, a broker topic) understands. It is
intentionally provider-agnostic: a logical-replication decode, a polling diff,
and a snapshot read all normalise into the *same* event so downstream code is
written once.

Design notes
------------
* **Position (LSN).** Every event carries a :class:`LogPosition` — a totally
  ordered, comparable position in the change log. For WAL it is the Postgres
  LSN; for polling it is a synthetic ``(updated_at, pk)`` cursor. Ordering and
  resumability are defined purely in terms of this position, so checkpointing
  is source-agnostic.
* **Snapshot vs. stream.** ``op == Op.READ`` marks a row observed during the
  initial consistent snapshot (Debezium calls this ``r``); ``INSERT`` /
  ``UPDATE`` / ``DELETE`` are live stream events. A consumer that only wants the
  steady state can ignore the boundary; one that needs exactly-once bootstrap
  uses :attr:`ChangeEvent.is_snapshot`.
* **Tombstones.** A delete is represented as an ``Op.DELETE`` event whose
  :attr:`ChangeEvent.before` holds the last-known row and :attr:`after` is
  ``None``. Compacted-topic semantics (a literal ``None`` value keyed by pk) are
  produced by :meth:`ChangeEvent.tombstone` for sinks that compact by key.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace
from typing import Any

JsonRow = dict[str, Any]


class Op(enum.StrEnum):
    """The kind of change a :class:`ChangeEvent` represents."""

    #: Row observed during the initial consistent snapshot (not a live change).
    READ = "r"
    INSERT = "c"
    UPDATE = "u"
    DELETE = "d"
    #: A schema change (DDL) decoded from the log; ``after`` carries the new
    #: column set. Lets the schema registry evolve in line with the stream.
    SCHEMA = "s"
    #: A periodic heartbeat carrying only a position; lets idle consumers
    #: advance their checkpoint without a real row change.
    HEARTBEAT = "h"


@dataclass(frozen=True, order=True, slots=True)
class LogPosition:
    """A totally ordered position in a change log.

    Two integers — ``(major, minor)`` — give a lexicographic order rich enough
    for both backends. For WAL: ``major`` is the LSN, ``minor`` always 0. For
    polling: ``major`` is the ``updated_at`` epoch-micros and ``minor`` is a
    monotonic tie-breaker so two rows with the same timestamp still order
    deterministically (by pk hash, assigned by the source).

    ``order=True`` makes the dataclass comparable, which is the entire point:
    checkpointing, dedup, and "have we passed the snapshot boundary" are all
    plain ``<=`` comparisons.
    """

    major: int = 0
    minor: int = 0

    @classmethod
    def zero(cls) -> LogPosition:
        """The position before any event (an empty checkpoint)."""
        return cls(0, 0)

    def next_minor(self) -> LogPosition:
        """The next position sharing this ``major`` (tie-break advance)."""
        return LogPosition(self.major, self.minor + 1)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.major}/{self.minor}"


@dataclass(frozen=True, slots=True)
class ChangeEvent:
    """One normalised change to one row of one table.

    Immutable so it can be fanned out to many consumers without defensive
    copies. ``before`` / ``after`` are plain JSON-able dicts keyed by column
    name; ``key`` is the primary-key projection used for routing, compaction and
    upsert idempotency.
    """

    #: Logical table identity (``"books"``, ``"entities"``, ...).
    table: str
    op: Op
    #: Position of this event in the source's change log.
    position: LogPosition
    #: Primary-key projection (``{"id": "book_42"}``) — never ``None`` for row
    #: events; empty for SCHEMA/HEARTBEAT.
    key: JsonRow = field(default_factory=dict)
    #: Row image *before* the change (UPDATE/DELETE); ``None`` for INSERT/READ.
    before: JsonRow | None = None
    #: Row image *after* the change (INSERT/UPDATE/READ); ``None`` for DELETE.
    after: JsonRow | None = None
    #: Source-assigned wall time the change was observed (epoch seconds).
    ts: float = 0.0
    #: Schema version the row image conforms to (bumped by SCHEMA events).
    schema_version: int = 1
    #: Free-form source metadata (txid, snapshot name, polling cursor, ...).
    meta: JsonRow = field(default_factory=dict)

    # -- predicates --------------------------------------------------------- #
    @property
    def is_snapshot(self) -> bool:
        """Whether this row came from the initial snapshot, not the live stream."""
        return self.op is Op.READ

    @property
    def is_delete(self) -> bool:
        return self.op is Op.DELETE

    @property
    def is_row_event(self) -> bool:
        """Whether this event mutates view state (insert/update/delete/read)."""
        return self.op in (Op.READ, Op.INSERT, Op.UPDATE, Op.DELETE)

    @property
    def row(self) -> JsonRow | None:
        """The "current" row image: ``after`` for upserts, ``before`` for deletes."""
        return self.before if self.is_delete else self.after

    # -- constructors ------------------------------------------------------- #
    @classmethod
    def insert(
        cls,
        table: str,
        after: JsonRow,
        position: LogPosition,
        *,
        key_columns: tuple[str, ...] = ("id",),
        ts: float = 0.0,
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        return cls(
            table=table,
            op=Op.INSERT,
            position=position,
            key=_project(after, key_columns),
            after=dict(after),
            ts=ts,
            schema_version=schema_version,
            meta=meta or {},
        )

    @classmethod
    def update(
        cls,
        table: str,
        before: JsonRow | None,
        after: JsonRow,
        position: LogPosition,
        *,
        key_columns: tuple[str, ...] = ("id",),
        ts: float = 0.0,
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        return cls(
            table=table,
            op=Op.UPDATE,
            position=position,
            key=_project(after, key_columns),
            before=dict(before) if before is not None else None,
            after=dict(after),
            ts=ts,
            schema_version=schema_version,
            meta=meta or {},
        )

    @classmethod
    def delete(
        cls,
        table: str,
        before: JsonRow,
        position: LogPosition,
        *,
        key_columns: tuple[str, ...] = ("id",),
        ts: float = 0.0,
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        return cls(
            table=table,
            op=Op.DELETE,
            position=position,
            key=_project(before, key_columns),
            before=dict(before),
            after=None,
            ts=ts,
            schema_version=schema_version,
            meta=meta or {},
        )

    @classmethod
    def read(
        cls,
        table: str,
        row: JsonRow,
        position: LogPosition,
        *,
        key_columns: tuple[str, ...] = ("id",),
        ts: float = 0.0,
        schema_version: int = 1,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        """A snapshot read (Debezium ``r``)."""
        return cls(
            table=table,
            op=Op.READ,
            position=position,
            key=_project(row, key_columns),
            after=dict(row),
            ts=ts,
            schema_version=schema_version,
            meta=meta or {},
        )

    @classmethod
    def heartbeat(cls, position: LogPosition, *, ts: float = 0.0) -> ChangeEvent:
        return cls(table="", op=Op.HEARTBEAT, position=position, ts=ts)

    @classmethod
    def schema(
        cls,
        table: str,
        columns: JsonRow,
        position: LogPosition,
        *,
        schema_version: int,
        ts: float = 0.0,
        meta: JsonRow | None = None,
    ) -> ChangeEvent:
        """A DDL/schema-change event; ``after`` carries the new column map."""
        return cls(
            table=table,
            op=Op.SCHEMA,
            position=position,
            after=dict(columns),
            ts=ts,
            schema_version=schema_version,
            meta=meta or {},
        )

    # -- transforms --------------------------------------------------------- #
    def tombstone(self) -> ChangeEvent:
        """A compaction tombstone for this key (``after`` cleared)."""
        return replace(self, op=Op.DELETE, before=self.before, after=None)

    def to_dict(self) -> JsonRow:
        """A JSON-serialisable envelope (for sinks / broker payloads)."""
        return {
            "table": self.table,
            "op": str(self.op),
            "position": [self.position.major, self.position.minor],
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "ts": self.ts,
            "schema_version": self.schema_version,
            "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, payload: JsonRow) -> ChangeEvent:
        pos = payload.get("position") or [0, 0]
        return cls(
            table=payload["table"],
            op=Op(payload["op"]),
            position=LogPosition(int(pos[0]), int(pos[1])),
            key=payload.get("key") or {},
            before=payload.get("before"),
            after=payload.get("after"),
            ts=float(payload.get("ts", 0.0)),
            schema_version=int(payload.get("schema_version", 1)),
            meta=payload.get("meta") or {},
        )


def _project(row: JsonRow, columns: tuple[str, ...]) -> JsonRow:
    """Project ``row`` onto ``columns`` (the primary-key tuple)."""
    return {c: row.get(c) for c in columns}


def key_str(key: JsonRow) -> str:
    """A stable string for a key dict, for use as a map/dedup/compaction key."""
    return "|".join(f"{k}={key[k]!r}" for k in sorted(key))


__all__ = ["ChangeEvent", "JsonRow", "LogPosition", "Op", "key_str"]
