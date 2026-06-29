"""In-memory event store, snapshot store, outbox + inbox — the test substrate.

A complete, spec-correct implementation of every store protocol, holding all
state in process memory behind an :class:`asyncio.Lock`. It exists so the domain
and projection facets (and this package's own conformance suite) can be tested
with **zero infrastructure** while exercising the *exact* ordering and
optimistic-concurrency semantics the Postgres store guarantees.

Semantics intentionally mirror :class:`PostgresEventStore`:

* gap-free global positions starting at 1,
* dense 0-based per-stream versions,
* the shared :func:`versioning.check` optimistic-concurrency decision,
* idempotent re-append of a known ``event_id`` (no-op, returns the stored event),
* transactional outbox rows written atomically with the append.

The lock makes a single append atomic; that is sufficient because everything is
single-process. There is no real rollback, so an append validates *fully* before
mutating any state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.eventsourcing.store.contracts import (
    EventData,
    EventSerializer,
    OutboxRecord,
    OutboxStatus,
    RecordedEvent,
    Snapshot,
    StreamSlice,
    new_event_id,
)
from app.eventsourcing.store.errors import AppendError
from app.eventsourcing.store.serialization import JsonEventSerializer
from app.eventsourcing.store.versioning import (
    NO_EVENTS,
    ExpectedVersion,
    check,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class InMemoryEventStore:
    """An in-process :class:`~contracts.EventStore` with snapshot/outbox/inbox.

    One instance is one logical store; share it across the components under test
    so they observe a consistent global order.
    """

    def __init__(self, serializer: EventSerializer | None = None) -> None:
        self._serializer: EventSerializer = serializer or JsonEventSerializer()
        # global log, in append order — index+1 is the global position.
        self._log: list[RecordedEvent] = []
        # stream_id -> list of RecordedEvent (version == list index).
        self._streams: dict[str, list[RecordedEvent]] = {}
        # event_id -> RecordedEvent (idempotent-append dedup).
        self._by_event_id: dict[str, RecordedEvent] = {}
        # snapshots: (stream_id, snapshot_type) -> latest Snapshot.
        self._snapshots: dict[tuple[str, str], Snapshot] = {}
        # outbox rows by id, in insertion order.
        self._outbox: dict[str, OutboxRecord] = {}
        # inbox: (consumer, message_id) -> result.
        self._inbox: dict[tuple[str, str], dict[str, Any] | None] = {}
        self._lock = asyncio.Lock()

    # -- EventStore --------------------------------------------------------- #

    async def append(
        self,
        stream_id: str,
        events: Sequence[EventData],
        *,
        expected_version: ExpectedVersion,
        publish_topic: str | None = None,
    ) -> tuple[RecordedEvent, ...]:
        if not events:
            raise AppendError("cannot append an empty batch")
        async with self._lock:
            # Idempotent re-append: if the *whole* batch is already stored under
            # its event_ids, return the stored events unchanged (a retried write).
            stored = [self._by_event_id.get(e.event_id) for e in events]
            if all(s is not None for s in stored):
                same_stream = all(s is not None and s.stream_id == stream_id for s in stored)
                if same_stream:
                    return tuple(s for s in stored if s is not None)

            stream = self._streams.setdefault(stream_id, [])
            current = len(stream) - 1  # NO_EVENTS (-1) when empty
            check(stream_id, expected_version, current if stream else NO_EVENTS)

            # Validate / JSON-safety the whole batch *before* mutating anything.
            serialized = [(e, self._serializer.serialize(e)) for e in events]
            for e in events:
                if e.event_id in self._by_event_id:
                    # A partial-overlap batch is a programming error.
                    raise AppendError(f"event_id {e.event_id!r} already exists (partial re-append)")

            recorded: list[RecordedEvent] = []
            for offset, (e, payload) in enumerate(serialized):
                version = len(stream) + offset
                global_position = len(self._log) + offset + 1
                rec = RecordedEvent(
                    stream_id=stream_id,
                    event_id=e.event_id,
                    event_type=e.event_type,
                    version=version,
                    global_position=global_position,
                    payload=payload,
                    metadata=e.metadata,
                    recorded_at=_utcnow(),
                )
                recorded.append(rec)

            # Commit: append to log + stream + index, then outbox rows.
            for rec in recorded:
                self._log.append(rec)
                self._streams[stream_id].append(rec)
                self._by_event_id[rec.event_id] = rec
                if publish_topic is not None:
                    ob = OutboxRecord(
                        id=new_event_id(),
                        event_id=rec.event_id,
                        global_position=rec.global_position,
                        topic=publish_topic,
                        payload={
                            "stream_id": rec.stream_id,
                            "event_id": rec.event_id,
                            "event_type": rec.event_type,
                            "version": rec.version,
                            "global_position": rec.global_position,
                            "payload": rec.payload,
                            "metadata": rec.metadata.to_dict(),
                        },
                        status=OutboxStatus.PENDING,
                        attempts=0,
                        available_at=rec.recorded_at,
                        created_at=rec.recorded_at,
                    )
                    self._outbox[ob.id] = ob
            return tuple(recorded)

    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 0,
        limit: int | None = None,
    ) -> StreamSlice:
        if from_version < 0:
            raise ValueError("from_version must be >= 0")
        async with self._lock:
            stream = self._streams.get(stream_id, [])
            tail = stream[from_version:]
            window = tail if limit is None else tail[:limit]
            events = tuple(self._deserialize(e) for e in window)
            last_version = events[-1].version if events else NO_EVENTS
            reached_tail = (from_version + len(window)) >= len(stream)
            return StreamSlice(
                stream_id=stream_id,
                events=events,
                last_version=last_version,
                is_end=reached_tail,
            )

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 100,
    ) -> tuple[RecordedEvent, ...]:
        if from_position < 0:
            raise ValueError("from_position must be >= 0")
        if limit <= 0:
            raise ValueError("limit must be >= 1")
        async with self._lock:
            # global_position is 1-based and dense → slice directly.
            window = self._log[from_position : from_position + limit]
            return tuple(self._deserialize(e) for e in window)

    async def stream_version(self, stream_id: str) -> int:
        async with self._lock:
            stream = self._streams.get(stream_id)
            return (len(stream) - 1) if stream else NO_EVENTS

    async def last_position(self) -> int:
        async with self._lock:
            return len(self._log)

    # -- SnapshotStore ------------------------------------------------------ #

    async def save(self, snapshot: Snapshot) -> None:
        async with self._lock:
            key = (snapshot.stream_id, snapshot.snapshot_type)
            existing = self._snapshots.get(key)
            if existing is None or snapshot.version >= existing.version:
                self._snapshots[key] = snapshot

    async def load_latest(
        self, stream_id: str, *, snapshot_type: str = "default"
    ) -> Snapshot | None:
        async with self._lock:
            return self._snapshots.get((stream_id, snapshot_type))

    # -- OutboxRepository --------------------------------------------------- #

    async def claim_batch(
        self, *, limit: int, now: datetime | None = None
    ) -> list[OutboxRecord]:
        now = now or _utcnow()
        async with self._lock:
            due = [
                r
                for r in self._outbox.values()
                if r.status is OutboxStatus.PENDING and r.available_at <= now
            ]
            due.sort(key=lambda r: r.global_position)
            return due[:limit]

    async def mark_published(
        self, ids: Sequence[str], *, now: datetime | None = None
    ) -> None:
        now = now or _utcnow()
        async with self._lock:
            for rid in ids:
                cur = self._outbox.get(rid)
                if cur is not None:
                    self._outbox[rid] = replace(
                        cur, status=OutboxStatus.PUBLISHED, published_at=now
                    )

    async def mark_failed(
        self,
        record_id: str,
        *,
        error: str,
        retry_at: datetime,
        dead: bool,
    ) -> None:
        async with self._lock:
            cur = self._outbox.get(record_id)
            if cur is None:
                return
            self._outbox[record_id] = replace(
                cur,
                status=OutboxStatus.DEAD if dead else OutboxStatus.PENDING,
                attempts=cur.attempts + 1,
                available_at=retry_at,
                last_error=error,
            )

    # -- InboxRepository ---------------------------------------------------- #

    async def already_processed(self, consumer: str, message_id: str) -> bool:
        async with self._lock:
            return (consumer, message_id) in self._inbox

    async def mark_processed(
        self,
        consumer: str,
        message_id: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> bool:
        async with self._lock:
            key = (consumer, message_id)
            if key in self._inbox:
                return False
            self._inbox[key] = result
            return True

    # -- test introspection (not part of any protocol) ---------------------- #

    def all_outbox(self) -> tuple[OutboxRecord, ...]:
        """Every outbox row (for assertions in tests)."""
        return tuple(self._outbox.values())

    def event_count(self) -> int:
        return len(self._log)

    # -- internals ---------------------------------------------------------- #

    def _deserialize(self, rec: RecordedEvent) -> RecordedEvent:
        payload = self._serializer.deserialize(rec.event_type, rec.payload)
        if payload is rec.payload:
            return rec
        return RecordedEvent(
            stream_id=rec.stream_id,
            event_id=rec.event_id,
            event_type=rec.event_type,
            version=rec.version,
            global_position=rec.global_position,
            payload=payload,
            metadata=rec.metadata,
            recorded_at=rec.recorded_at,
        )


__all__ = ["InMemoryEventStore"]
