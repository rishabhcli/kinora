"""The Postgres-backed event store + outbox repository.

:class:`PostgresEventStore` implements the :class:`~contracts.EventStore` (and
:class:`~contracts.SnapshotStore`) protocols over an :class:`AsyncSession`. Like
every Kinora repository it **only flushes** — the caller's unit of work owns the
commit. That is exactly what lets the *domain* facet append events and write its
own read-model rows in one atomic transaction.

Atomicity & ordering:

* Global positions are allocated gap-free via :mod:`sequence` inside the same
  transaction (a rollback returns the numbers).
* Per-stream versions are computed against the current max version, guarded by
  the shared :func:`versioning.check` and backstopped by the unique
  ``(stream_id, version)`` constraint — so even two writers that both read the
  same current version cannot both commit (the second hits the constraint and is
  re-raised as :class:`OptimisticConcurrencyError`).
* When ``publish_topic`` is given, an ``es_outbox`` row per event is written in
  the same flush — the transactional outbox.

:class:`PostgresOutboxRepository` is the relay-facing claim/mark side, using
``FOR UPDATE SKIP LOCKED`` so multiple relay workers never claim the same row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.eventsourcing.store import sequence
from app.eventsourcing.store.contracts import (
    EventData,
    EventMetadata,
    EventSerializer,
    OutboxRecord,
    OutboxStatus,
    RecordedEvent,
    Snapshot,
    StreamSlice,
    new_event_id,
)
from app.eventsourcing.store.errors import (
    AppendError,
    OptimisticConcurrencyError,
)
from app.eventsourcing.store.models import (
    EventStoreEvent,
    EventStoreOutbox,
    EventStoreSnapshot,
)
from app.eventsourcing.store.serialization import JsonEventSerializer
from app.eventsourcing.store.versioning import (
    NO_EVENTS,
    ExpectedVersion,
    check,
    describe,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PostgresEventStore:
    """A Postgres :class:`~contracts.EventStore` bound to one session."""

    def __init__(self, session: AsyncSession, *, serializer: EventSerializer | None = None) -> None:
        self.session = session
        self._serializer: EventSerializer = serializer or JsonEventSerializer()

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

        # Idempotent re-append: if the whole batch's event_ids are already stored
        # in this stream, return them unchanged (a successfully-committed retry).
        existing = await self._lookup_existing(stream_id, [e.event_id for e in events])
        if len(existing) == len(events):
            return tuple(existing[e.event_id] for e in events)
        if existing:
            # Partial overlap: a programming error (mixed new + already-stored ids).
            raise AppendError(
                f"partial re-append on stream {stream_id!r}: "
                f"{len(existing)}/{len(events)} event ids already exist"
            )

        current = await self.stream_version(stream_id)
        check(stream_id, expected_version, current)

        # Validate/serialise the whole batch before allocating positions.
        serialized = [(e, self._serializer.serialize(e)) for e in events]
        start_pos = await sequence.allocate(self.session, len(events))

        now = _utcnow()
        recorded: list[RecordedEvent] = []
        for offset, (e, payload) in enumerate(serialized):
            version = current + 1 + offset
            global_position = start_pos + offset
            metadata = e.metadata
            row = EventStoreEvent(
                global_position=global_position,
                event_id=e.event_id,
                stream_id=stream_id,
                version=version,
                event_type=e.event_type,
                payload=payload,
                event_metadata=metadata.to_dict(),
                correlation_id=metadata.correlation_id,
                recorded_at=now,
            )
            self.session.add(row)
            recorded.append(
                RecordedEvent(
                    stream_id=stream_id,
                    event_id=e.event_id,
                    event_type=e.event_type,
                    version=version,
                    global_position=global_position,
                    payload=payload,
                    metadata=metadata,
                    recorded_at=now,
                )
            )
            if publish_topic is not None:
                self.session.add(
                    EventStoreOutbox(
                        id=new_event_id(),
                        event_id=e.event_id,
                        global_position=global_position,
                        topic=publish_topic,
                        payload={
                            "stream_id": stream_id,
                            "event_id": e.event_id,
                            "event_type": e.event_type,
                            "version": version,
                            "global_position": global_position,
                            "payload": payload,
                            "metadata": metadata.to_dict(),
                        },
                        status=OutboxStatus.PENDING.value,
                        attempts=0,
                        available_at=now,
                        created_at=now,
                    )
                )

        try:
            await self.session.flush()
        except IntegrityError as exc:
            # The unique (stream_id, version) or event_id backstop tripped — a
            # concurrent writer beat us past the read-check. Surface as OCC so the
            # caller retries. The session is now in a failed state; the unit of
            # work will roll it back.
            raise OptimisticConcurrencyError(
                stream_id,
                expected=describe(expected_version),
                actual=None,
            ) from exc
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
        stmt = (
            select(EventStoreEvent)
            .where(
                EventStoreEvent.stream_id == stream_id,
                EventStoreEvent.version >= from_version,
            )
            .order_by(EventStoreEvent.version.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        events = tuple(self._to_recorded(r) for r in rows)
        last_version = events[-1].version if events else NO_EVENTS
        current = await self.stream_version(stream_id)
        reached_tail = last_version == current or not events and from_version > current
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
        stmt = (
            select(EventStoreEvent)
            .where(EventStoreEvent.global_position > from_position)
            .order_by(EventStoreEvent.global_position.asc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return tuple(self._to_recorded(r) for r in rows)

    async def stream_version(self, stream_id: str) -> int:
        row = (
            await self.session.execute(
                select(func.max(EventStoreEvent.version)).where(
                    EventStoreEvent.stream_id == stream_id
                )
            )
        ).scalar_one_or_none()
        return NO_EVENTS if row is None else int(row)

    async def last_position(self) -> int:
        row = (
            await self.session.execute(select(func.max(EventStoreEvent.global_position)))
        ).scalar_one_or_none()
        return 0 if row is None else int(row)

    # -- SnapshotStore ------------------------------------------------------ #

    async def save(self, snapshot: Snapshot) -> None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(EventStoreSnapshot)
            .values(
                stream_id=snapshot.stream_id,
                snapshot_type=snapshot.snapshot_type,
                version=snapshot.version,
                state=snapshot.state,
                created_at=snapshot.created_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    EventStoreSnapshot.stream_id,
                    EventStoreSnapshot.snapshot_type,
                ],
                set_={
                    "version": snapshot.version,
                    "state": snapshot.state,
                    "created_at": snapshot.created_at,
                },
                # Only replace with a newer-or-equal version (monotone snapshots).
                where=EventStoreSnapshot.version <= snapshot.version,
            )
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def load_latest(
        self, stream_id: str, *, snapshot_type: str = "default"
    ) -> Snapshot | None:
        row = (
            await self.session.execute(
                select(EventStoreSnapshot).where(
                    EventStoreSnapshot.stream_id == stream_id,
                    EventStoreSnapshot.snapshot_type == snapshot_type,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return Snapshot(
            stream_id=row.stream_id,
            version=row.version,
            state=dict(row.state),
            snapshot_type=row.snapshot_type,
            created_at=row.created_at,
        )

    # -- internals ---------------------------------------------------------- #

    async def _lookup_existing(
        self, stream_id: str, event_ids: Sequence[str]
    ) -> dict[str, RecordedEvent]:
        rows = (
            await self.session.execute(
                select(EventStoreEvent).where(
                    EventStoreEvent.stream_id == stream_id,
                    EventStoreEvent.event_id.in_(list(event_ids)),
                )
            )
        ).scalars().all()
        return {r.event_id: self._to_recorded(r) for r in rows}

    def _to_recorded(self, row: EventStoreEvent) -> RecordedEvent:
        payload = self._serializer.deserialize(row.event_type, dict(row.payload))
        return RecordedEvent(
            stream_id=row.stream_id,
            event_id=row.event_id,
            event_type=row.event_type,
            version=row.version,
            global_position=row.global_position,
            payload=payload,
            metadata=EventMetadata.from_dict(dict(row.event_metadata)),
            recorded_at=row.recorded_at,
        )


class PostgresOutboxRepository:
    """Relay-facing claim/mark side of the transactional outbox (``es_outbox``)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def claim_batch(
        self, *, limit: int, now: datetime | None = None
    ) -> list[OutboxRecord]:
        now = now or _utcnow()
        # FOR UPDATE SKIP LOCKED lets several relay workers drain in parallel
        # without ever claiming the same row.
        stmt = (
            select(EventStoreOutbox)
            .where(
                EventStoreOutbox.status == OutboxStatus.PENDING.value,
                EventStoreOutbox.available_at <= now,
            )
            .order_by(EventStoreOutbox.global_position.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [self._to_record(r) for r in rows]

    async def mark_published(self, ids: Sequence[str], *, now: datetime | None = None) -> None:
        if not ids:
            return
        now = now or _utcnow()
        await self.session.execute(
            update(EventStoreOutbox)
            .where(EventStoreOutbox.id.in_(list(ids)))
            .values(status=OutboxStatus.PUBLISHED.value, published_at=now)
        )
        await self.session.flush()

    async def mark_failed(
        self,
        record_id: str,
        *,
        error: str,
        retry_at: datetime,
        dead: bool,
    ) -> None:
        await self.session.execute(
            update(EventStoreOutbox)
            .where(EventStoreOutbox.id == record_id)
            .values(
                status=OutboxStatus.DEAD.value if dead else OutboxStatus.PENDING.value,
                attempts=EventStoreOutbox.attempts + 1,
                available_at=retry_at,
                last_error=error,
            )
        )
        await self.session.flush()

    def _to_record(self, row: EventStoreOutbox) -> OutboxRecord:
        return OutboxRecord(
            id=row.id,
            event_id=row.event_id,
            global_position=row.global_position,
            topic=row.topic,
            payload=dict(row.payload),
            status=OutboxStatus(row.status),
            attempts=row.attempts,
            available_at=row.available_at,
            created_at=row.created_at,
            published_at=row.published_at,
            last_error=row.last_error,
        )


__all__ = ["PostgresEventStore", "PostgresOutboxRepository"]
