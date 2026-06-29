"""The aggregate repository — the one bridge between aggregates and the store.

Responsibilities, all behind the :class:`~app.eventsourcing.store.EventStore`
protocol:

* **load** — read a stream, deserialise its envelopes (running upcasters), and
  :meth:`~app.eventsourcing.domain.aggregate.AggregateRoot.replay` them into a
  fresh aggregate instance built by the registered factory;
* **save** — serialise an aggregate's uncommitted events (stamping each with the
  command's metadata) and append them with the aggregate's
  :attr:`~app.eventsourcing.domain.aggregate.AggregateRoot.expected_version` as the
  optimistic-concurrency token, then :meth:`mark_committed`.

The repository is generic over the aggregate type and takes a *factory*
(``aggregate_id -> AggregateRoot``) so each aggregate kind registers its own.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Generic, TypeVar

from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.events import (
    DomainEvent,
    EventMetadata,
    EventRegistry,
    deserialise,
    registry,
    serialise,
)
from app.eventsourcing.domain.identifiers import StreamId
from app.eventsourcing.domain.snapshotting import SnapshotPolicy, Snapshotter
from app.eventsourcing.domain.upcasting import UpcasterRegistry, upcasters
from app.eventsourcing.store.protocol import AppendResult, EventStore
from app.eventsourcing.store.snapshots import Snapshot, SnapshotStore

_A = TypeVar("_A", bound=AggregateRoot)

#: Builds a blank aggregate from its id (before replaying history into it).
AggregateFactory = Callable[[str], _A]


@dataclass(slots=True)
class Repository(Generic[_A]):
    """Loads and persists one aggregate kind through an :class:`EventStore`.

    Args:
        store: the event store (facet A).
        factory: builds a blank aggregate for an id.
        event_registry: maps stored ``type`` -> event class (defaults to the
            process registry).
        upcaster_registry: migrates old event versions on load (defaults to the
            process registry).
        snapshot_store: optional snapshot persistence (facet A). When set *and*
            the aggregate implements :class:`~app.eventsourcing.domain.snapshotting.Snapshotter`,
            loads restore the latest snapshot and replay only the tail, and saves
            write a fresh snapshot per :attr:`snapshot_policy`.
        snapshot_policy: decides when a save writes a new snapshot.
    """

    store: EventStore
    factory: AggregateFactory[_A]
    event_registry: EventRegistry = registry
    upcaster_registry: UpcasterRegistry = upcasters
    snapshot_store: SnapshotStore | None = None
    snapshot_policy: SnapshotPolicy = field(default_factory=SnapshotPolicy)

    async def load(self, aggregate_id: str) -> _A:
        """Rebuild an aggregate, restoring from a snapshot + tail replay if able.

        With no snapshot store (or a non-snapshottable aggregate) this is a full
        replay from event 1. With both, it loads the latest snapshot, restores it,
        and replays only events with ``version > snapshot.version``.
        """
        agg = self.factory(aggregate_id)
        stream_id = agg.stream_id.value
        from_version = 0
        if self.snapshot_store is not None and isinstance(agg, Snapshotter):
            snapshot = await self.snapshot_store.load(stream_id)
            if snapshot is not None:
                agg.restore_state(snapshot.state, version=snapshot.version)
                from_version = snapshot.version
        stored = await self.store.load(stream_id, from_version=from_version)
        events = [
            self._deserialise(env.event_type, env.event_version, env.payload) for env in stored
        ]
        agg.replay(events)
        return agg

    async def save(
        self,
        aggregate: _A,
        *,
        metadata: EventMetadata | None = None,
    ) -> AppendResult:
        """Append the aggregate's uncommitted events with optimistic concurrency.

        Stamps each event with ``metadata`` (the bus supplies a per-event copy via
        :meth:`stamp` when finer provenance is needed). On success, marks the
        aggregate committed.

        Raises:
            ConcurrencyError: the store moved past ``aggregate.expected_version``.
        """
        pending = aggregate.uncommitted
        if not pending:
            current = await self.store.current_version(aggregate.stream_id.value)
            return AppendResult(aggregate.stream_id.value, current, current)
        version_before = aggregate.expected_version
        envelopes = [serialise(e, metadata) for e in pending]
        result = await self.store.append(
            aggregate.stream_id.value,
            envelopes,
            expected_version=aggregate.expected_version,
        )
        aggregate.mark_committed()
        await self._maybe_snapshot(aggregate, version_before, result.last_version)
        return result

    async def save_with_metadata(
        self,
        aggregate: _A,
        per_event_metadata: Sequence[EventMetadata],
    ) -> AppendResult:
        """Append uncommitted events each carrying its own metadata envelope.

        ``per_event_metadata`` must align 1:1 with ``aggregate.uncommitted`` order.
        Used by the bus to stamp each event with a distinct ``event_id``.
        """
        pending = aggregate.uncommitted
        if len(per_event_metadata) != len(pending):
            raise ValueError(
                f"metadata count {len(per_event_metadata)} != " f"uncommitted count {len(pending)}"
            )
        if not pending:
            current = await self.store.current_version(aggregate.stream_id.value)
            return AppendResult(aggregate.stream_id.value, current, current)
        version_before = aggregate.expected_version
        envelopes = [serialise(e, m) for e, m in zip(pending, per_event_metadata, strict=True)]
        result = await self.store.append(
            aggregate.stream_id.value,
            envelopes,
            expected_version=aggregate.expected_version,
        )
        aggregate.mark_committed()
        await self._maybe_snapshot(aggregate, version_before, result.last_version)
        return result

    async def current_version(self, stream_id: StreamId) -> int:
        return await self.store.current_version(stream_id.value)

    async def _maybe_snapshot(self, aggregate: _A, version_before: int, version_after: int) -> None:
        """Write a snapshot when the policy says to and the aggregate supports it."""
        if self.snapshot_store is None or not isinstance(aggregate, Snapshotter):
            return
        if not self.snapshot_policy.should_snapshot(version_before, version_after):
            return
        await self.snapshot_store.save(
            Snapshot(
                stream_id=aggregate.stream_id.value,
                version=version_after,
                state=aggregate.snapshot_state(),
            )
        )

    def _deserialise(self, event_type: str, event_version: int, payload: object) -> DomainEvent:
        event, _meta = deserialise(
            {"type": event_type, "version": event_version, "data": payload},
            event_registry=self.event_registry,
            upcasters=self.upcaster_registry,
        )
        return event


__all__ = ["AggregateFactory", "Repository"]
