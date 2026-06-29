"""Aggregate rehydration — snapshot + event-replay, the domain-facet building block.

The *domain* facet (the sibling agent) models each aggregate as a fold over its
stream: ``state = reduce(apply, events, initial)``. This module gives that fold a
reusable, snapshot-accelerated driver so the domain facet does not re-implement
"load latest snapshot, replay the tail, decide whether to snapshot" for every
aggregate type.

It is intentionally **state-shape-agnostic**: the caller supplies

* ``initial()`` — the empty state,
* ``apply(state, event)`` — fold one :class:`RecordedEvent` into the state,
* ``serialize(state)`` / ``deserialize(dict)`` — to/from the snapshot's JSON.

`Aggregate[S]` bundles those four functions (a pure "definition" of how a stream
becomes state). :class:`AggregateRepository` then:

* **loads** an aggregate: latest snapshot (≤ some version) folded with the events
  *after* it — O(events-since-snapshot), not O(stream length);
* **records new events** with optimistic concurrency at the loaded version;
* **snapshots** when the :class:`SnapshotStrategy` says to, in the same flow.

This keeps the store generic while handing the domain facet a turnkey
event-sourced repository. Everything is pure except the awaited store/snapshot
calls (which the caller's unit of work commits).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.eventsourcing.store.contracts import (
    EventData,
    EventStore,
    RecordedEvent,
    Snapshot,
    SnapshotStore,
)
from app.eventsourcing.store.snapshot import SnapshotStrategy
from app.eventsourcing.store.versioning import NO_EVENTS, ExpectedVersion

S = TypeVar("S")


@dataclass(frozen=True, slots=True)
class Aggregate(Generic[S]):
    """A pure definition of how a stream of events folds into state ``S``.

    ``snapshot_type`` namespaces this aggregate's snapshots in the snapshot store
    so two aggregate definitions over the same stream id never collide.
    """

    initial: Callable[[], S]
    apply: Callable[[S, RecordedEvent], S]
    serialize: Callable[[S], dict]
    deserialize: Callable[[dict], S]
    snapshot_type: str = "default"

    def fold(self, state: S, events: Sequence[RecordedEvent]) -> S:
        for event in events:
            state = self.apply(state, event)
        return state


@dataclass(frozen=True, slots=True)
class LoadedAggregate(Generic[S]):
    """The result of loading an aggregate: its state + the version it reflects.

    ``version`` is the stream version of the last applied event (:data:`NO_EVENTS`
    for a never-written aggregate); pass it back as ``expected_version`` to
    :meth:`AggregateRepository.append` for a lost-update-safe write.
    """

    stream_id: str
    state: S
    version: int

    @property
    def exists(self) -> bool:
        return self.version > NO_EVENTS


class AggregateRepository(Generic[S]):
    """Loads / appends an event-sourced aggregate with snapshot acceleration."""

    def __init__(
        self,
        aggregate: Aggregate[S],
        store: EventStore,
        *,
        snapshots: SnapshotStore | None = None,
        snapshot_strategy: SnapshotStrategy | None = None,
        read_batch: int = 500,
    ) -> None:
        if read_batch <= 0:
            raise ValueError("read_batch must be >= 1")
        self._agg = aggregate
        self._store = store
        self._snapshots = snapshots
        self._strategy = snapshot_strategy
        self._read_batch = read_batch

    async def load(self, stream_id: str) -> LoadedAggregate[S]:
        """Rehydrate ``stream_id``: snapshot (if any) folded with the tail events."""
        state = self._agg.initial()
        from_version = 0
        snapshot_version = NO_EVENTS

        if self._snapshots is not None:
            snap = await self._snapshots.load_latest(
                stream_id, snapshot_type=self._agg.snapshot_type
            )
            if snap is not None:
                state = self._agg.deserialize(snap.state)
                from_version = snap.version + 1
                snapshot_version = snap.version

        version = snapshot_version
        # Page the tail so a very long stream never loads unbounded into memory.
        while True:
            slice_ = await self._store.read_stream(
                stream_id, from_version=from_version, limit=self._read_batch
            )
            if slice_.events:
                state = self._agg.fold(state, slice_.events)
                version = slice_.events[-1].version
                from_version = version + 1
            if slice_.is_end or not slice_.events:
                break

        return LoadedAggregate(stream_id=stream_id, state=state, version=version)

    async def append(
        self,
        stream_id: str,
        events: Sequence[EventData],
        *,
        expected_version: ExpectedVersion,
        publish_topic: str | None = None,
    ) -> LoadedAggregate[S]:
        """Append ``events`` under OCC, returning the new folded state + version.

        When a :class:`SnapshotStrategy` + :class:`SnapshotStore` are configured
        and the strategy says so, a fresh snapshot of the *resulting* state is
        written in the same flow (the caller's unit of work commits both).
        """
        await self._store.append(
            stream_id,
            events,
            expected_version=expected_version,
            publish_topic=publish_topic,
        )
        # Reload (snapshot-accelerated) to fold the new tail onto the prior state.
        # For the common "append after load" flow the snapshot makes this O(new
        # events); the round-trip keeps a single source of truth for the fold.
        loaded = await self.load(stream_id)
        await self._maybe_snapshot(loaded)
        return loaded

    async def _maybe_snapshot(self, loaded: LoadedAggregate[S]) -> None:
        if self._snapshots is None or self._strategy is None or not loaded.exists:
            return
        last_snap_version = NO_EVENTS
        existing = await self._snapshots.load_latest(
            loaded.stream_id, snapshot_type=self._agg.snapshot_type
        )
        if existing is not None:
            last_snap_version = existing.version
        if self._strategy.should_snapshot(
            new_version=loaded.version, last_snapshot_version=last_snap_version
        ):
            await self._snapshots.save(
                Snapshot(
                    stream_id=loaded.stream_id,
                    version=loaded.version,
                    state=self._agg.serialize(loaded.state),
                    snapshot_type=self._agg.snapshot_type,
                )
            )

    async def snapshot_now(self, loaded: LoadedAggregate[S]) -> None:
        """Force a snapshot of ``loaded`` (ignores the strategy cadence)."""
        if self._snapshots is None:
            raise RuntimeError("no SnapshotStore configured")
        if not loaded.exists:
            return
        await self._snapshots.save(
            Snapshot(
                stream_id=loaded.stream_id,
                version=loaded.version,
                state=self._agg.serialize(loaded.state),
                snapshot_type=self._agg.snapshot_type,
            )
        )


__all__ = ["Aggregate", "AggregateRepository", "LoadedAggregate"]
