"""Postgres logical-replication (WAL) CDC source.

The primary, lowest-latency source: a logical replication slot streams every
committed change as a structured message. Kinora's Postgres runs ``pgvector``;
logical decoding with the ``wal2json`` (or ``pgoutput``) plugin turns the WAL
into JSON change records that map one-to-one onto :class:`ChangeEvent`.

This module decouples *decoding* (pure, fully testable) from *transport*
(reading the replication slot, which needs a live server and a privileged
connection). The decoder — :func:`decode_wal2json` — is the part that carries
the real logic and is exercised deterministically; :class:`PostgresLogicalSource`
wraps an injected :class:`WalReader` port so the same source object can be
driven by a fake reader in tests and by a real ``psycopg`` replication cursor in
production (the latter is a thin adapter the pipeline injects; it is not
imported here, keeping the unit suite infra-free).

wal2json record shape (the subset we consume)::

    {
      "lsn": "0/16B3748",
      "change": [
        {"kind": "insert", "table": "books",
         "columnnames": ["id", "title"], "columnvalues": ["b1", "Dune"]},
        {"kind": "update", "table": "books",
         "columnnames": [...], "columnvalues": [...],
         "oldkeys": {"keynames": ["id"], "keyvalues": ["b1"]}},
        {"kind": "delete", "table": "books",
         "oldkeys": {"keynames": ["id"], "keyvalues": ["b1"]}}
      ]
    }
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator, Iterable
from typing import Any

from app.streaming.cdc.clock import Clock, SystemClock
from app.streaming.cdc.events import ChangeEvent, JsonRow, LogPosition, Op
from app.streaming.cdc.source import CDCSource


def parse_lsn(lsn: str | int) -> int:
    """Parse a Postgres LSN (``"0/16B3748"`` or an int) into a sortable int."""
    if isinstance(lsn, int):
        return lsn
    if "/" in lsn:
        hi, lo = lsn.split("/", 1)
        return (int(hi, 16) << 32) | int(lo, 16)
    return int(lsn, 16) if lsn else 0


def decode_wal2json(
    record: dict[str, Any],
    *,
    key_columns_by_table: dict[str, tuple[str, ...]] | None = None,
    ts: float = 0.0,
) -> list[ChangeEvent]:
    """Decode one wal2json transaction record into ordered change events.

    Each ``change`` entry becomes one :class:`ChangeEvent`. The transaction LSN
    is the ``major``; the per-change index within the transaction is the
    ``minor`` so events from one commit stay totally ordered. Primary keys are
    taken from ``oldkeys`` (update/delete) or the table's declared key columns.
    """
    key_columns_by_table = key_columns_by_table or {}
    lsn = parse_lsn(record.get("lsn", 0) or 0)
    out: list[ChangeEvent] = []
    for i, change in enumerate(record.get("change", [])):
        table = change.get("table", "")
        kind = change.get("kind", "")
        key_columns = key_columns_by_table.get(table, ("id",))
        position = LogPosition(lsn, i)
        after = _columns_to_row(change)
        before = _oldkeys_to_row(change)
        if kind == "insert":
            out.append(
                ChangeEvent.insert(
                    table,
                    after or {},
                    position,
                    key_columns=key_columns,
                    ts=ts,
                    meta={"via": "wal2json"},
                )
            )
        elif kind == "update":
            out.append(
                ChangeEvent.update(
                    table,
                    before,
                    after or {},
                    position,
                    key_columns=key_columns,
                    ts=ts,
                    meta={"via": "wal2json"},
                )
            )
        elif kind == "delete":
            out.append(
                ChangeEvent.delete(
                    table,
                    before or {},
                    position,
                    key_columns=key_columns,
                    ts=ts,
                    meta={"via": "wal2json"},
                )
            )
        # 'message'/'truncate'/'begin'/'commit' kinds are ignored here.
    return out


def _columns_to_row(change: dict[str, Any]) -> JsonRow | None:
    names = change.get("columnnames")
    values = change.get("columnvalues")
    if not names or values is None:
        return None
    return dict(zip(names, values, strict=False))


def _oldkeys_to_row(change: dict[str, Any]) -> JsonRow | None:
    old = change.get("oldkeys")
    if not old:
        return None
    names = old.get("keynames", [])
    values = old.get("keyvalues", [])
    return dict(zip(names, values, strict=False))


class WalReader(abc.ABC):
    """Port over a logical-replication slot (real cursor or fake)."""

    @abc.abstractmethod
    def records(self, *, after_lsn: int) -> Iterable[dict[str, Any]]:
        """Yield wal2json transaction records with LSN strictly after ``after_lsn``."""
        raise NotImplementedError


class PostgresLogicalSource(CDCSource):
    """A :class:`CDCSource` over a logical-replication slot via a :class:`WalReader`.

    Decoding is delegated to :func:`decode_wal2json`; the source only sequences
    records, applies the ``after`` cutoff, and tracks the head position. The
    initial snapshot for WAL is taken by an *exported snapshot* in the same
    transaction that created the slot; that is a transport concern, so the
    snapshot here is delegated to an optional injected snapshot source (the
    pipeline pairs this with the polling source's snapshot or a dedicated
    ``COPY``-based reader). Absent one, this is a pure-stream source.
    """

    def __init__(
        self,
        reader: WalReader,
        *,
        key_columns_by_table: dict[str, tuple[str, ...]] | None = None,
        clock: Clock | None = None,
        snapshot_source: CDCSource | None = None,
    ) -> None:
        self._reader = reader
        self._key_columns_by_table = key_columns_by_table or {}
        self._clock = clock or SystemClock()
        self._snapshot_source = snapshot_source
        self._head = LogPosition.zero()

    async def snapshot(self) -> AsyncIterator[ChangeEvent]:
        if self._snapshot_source is None:
            return
        async for ev in self._snapshot_source.snapshot():
            yield ev

    async def stream(self, *, after: LogPosition | None = None) -> AsyncIterator[ChangeEvent]:
        cutoff = after or LogPosition.zero()
        for record in self._reader.records(after_lsn=cutoff.major):
            for ev in decode_wal2json(
                record,
                key_columns_by_table=self._key_columns_by_table,
                ts=self._clock.time(),
            ):
                if ev.position > cutoff:
                    self._head = ev.position
                    yield ev

    @property
    def head_position(self) -> LogPosition:
        return self._head


class ListWalReader(WalReader):
    """A deterministic in-memory :class:`WalReader` for tests."""

    def __init__(self, records: Iterable[dict[str, Any]] | None = None) -> None:
        self._records: list[dict[str, Any]] = list(records or [])

    def append(self, record: dict[str, Any]) -> None:
        self._records.append(record)

    def records(self, *, after_lsn: int) -> Iterable[dict[str, Any]]:
        for rec in self._records:
            if parse_lsn(rec.get("lsn", 0) or 0) > after_lsn:
                yield rec


__all__ = [
    "ListWalReader",
    "Op",
    "PostgresLogicalSource",
    "WalReader",
    "decode_wal2json",
    "parse_lsn",
]
