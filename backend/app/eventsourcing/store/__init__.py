"""Append-only event store (event-sourcing facet A).

The public seam the *domain* and *projection* facets import. See ``DESIGN.md``
for the architecture. Pure value objects + protocols come from :mod:`contracts`
and :mod:`versioning`; the two implementations are :class:`InMemoryEventStore`
(tests, zero infra) and :class:`PostgresEventStore` (production). The
:class:`OutboxRelay` is the reliable-publish poller; :class:`EventStoreFactory`
is the DI seam used by the composition root.

The Postgres implementation, ORM models, snapshot/inbox repos, and factory are
imported lazily so importing this package never imports SQLAlchemy/ORM models —
keeping the pure contracts cheap to import (matches the "lazy composition" rule).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.eventsourcing.store.aggregate import (
    Aggregate,
    AggregateRepository,
    LoadedAggregate,
)
from app.eventsourcing.store.checkpoint import InMemoryCheckpointStore
from app.eventsourcing.store.contracts import (
    Checkpoint,
    CheckpointStatus,
    CheckpointStore,
    EventData,
    EventMetadata,
    EventSerializer,
    EventStore,
    InboxRepository,
    MessagePublisher,
    OutboxRecord,
    OutboxRepository,
    OutboxStatus,
    RecordedEvent,
    Snapshot,
    SnapshotStore,
    StreamSlice,
    new_event_id,
)
from app.eventsourcing.store.errors import (
    AppendError,
    DuplicateEventError,
    EventStoreError,
    OptimisticConcurrencyError,
    SerializationError,
    StreamNotFoundError,
)
from app.eventsourcing.store.memory import InMemoryEventStore
from app.eventsourcing.store.outbox import OutboxRelay, RelayResult, backoff_delay
from app.eventsourcing.store.publishing import (
    CHANNEL_PREFIX,
    CollectingPublisher,
    RedisMessagePublisher,
    RoutingPublisher,
    channel_for,
)
from app.eventsourcing.store.serialization import (
    EventTypeRegistry,
    EventTypeSpec,
    JsonEventSerializer,
)
from app.eventsourcing.store.subscription import (
    CatchUpSubscription,
    EventHandler,
    SubscriptionResult,
)
from app.eventsourcing.store.versioning import (
    ANY,
    NO_EVENTS,
    NO_STREAM,
    STREAM_EXISTS,
    ExpectedVersion,
    StreamState,
)

if TYPE_CHECKING:  # avoid importing the ORM/SQLAlchemy layer eagerly
    from app.eventsourcing.store.checkpoint import PostgresCheckpointStore
    from app.eventsourcing.store.inbox import PostgresInboxRepository
    from app.eventsourcing.store.postgres import PostgresEventStore, PostgresOutboxRepository
    from app.eventsourcing.store.service import EventStoreFactory
    from app.eventsourcing.store.snapshot import PostgresSnapshotStore, SnapshotStrategy

_LAZY: dict[str, tuple[str, str]] = {
    "PostgresEventStore": ("app.eventsourcing.store.postgres", "PostgresEventStore"),
    "PostgresOutboxRepository": ("app.eventsourcing.store.postgres", "PostgresOutboxRepository"),
    "PostgresInboxRepository": ("app.eventsourcing.store.inbox", "PostgresInboxRepository"),
    "PostgresSnapshotStore": ("app.eventsourcing.store.snapshot", "PostgresSnapshotStore"),
    "PostgresCheckpointStore": ("app.eventsourcing.store.checkpoint", "PostgresCheckpointStore"),
    "SnapshotStrategy": ("app.eventsourcing.store.snapshot", "SnapshotStrategy"),
    "EventStoreFactory": ("app.eventsourcing.store.service", "EventStoreFactory"),
}


def __getattr__(name: str) -> Any:  # PEP 562 lazy import of the DB-bound symbols
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    return getattr(module, target[1])


__all__ = [
    "ANY",
    "NO_EVENTS",
    "NO_STREAM",
    "STREAM_EXISTS",
    "Aggregate",
    "AggregateRepository",
    "CHANNEL_PREFIX",
    "AppendError",
    "CatchUpSubscription",
    "Checkpoint",
    "CheckpointStatus",
    "CheckpointStore",
    "CollectingPublisher",
    "DuplicateEventError",
    "EventData",
    "EventHandler",
    "EventMetadata",
    "EventSerializer",
    "EventStore",
    "EventStoreError",
    "EventStoreFactory",
    "EventTypeRegistry",
    "EventTypeSpec",
    "ExpectedVersion",
    "InMemoryCheckpointStore",
    "InMemoryEventStore",
    "InboxRepository",
    "JsonEventSerializer",
    "LoadedAggregate",
    "MessagePublisher",
    "OptimisticConcurrencyError",
    "OutboxRecord",
    "OutboxRelay",
    "OutboxRepository",
    "OutboxStatus",
    "PostgresCheckpointStore",
    "PostgresEventStore",
    "PostgresInboxRepository",
    "PostgresOutboxRepository",
    "PostgresSnapshotStore",
    "RecordedEvent",
    "RedisMessagePublisher",
    "RelayResult",
    "RoutingPublisher",
    "SerializationError",
    "Snapshot",
    "SnapshotStore",
    "SnapshotStrategy",
    "StreamNotFoundError",
    "StreamSlice",
    "StreamState",
    "SubscriptionResult",
    "backoff_delay",
    "channel_for",
    "new_event_id",
]

# Facet-B (command/aggregate write side) seam compatibility: additively re-export
# the protocol names the domain facet was authored against. These are unique to
# facet B's stub protocol and do NOT shadow facet A's authoritative exports above.
from app.eventsourcing.store.protocol import (  # noqa: E402
    EVENT_STORE_BEGINNING,
    AppendResult,
    ConcurrencyError,
    StoredEvent,
)
from app.eventsourcing.store.snapshots import InMemorySnapshotStore  # noqa: E402

__all__ += [
    "EVENT_STORE_BEGINNING",
    "AppendResult",
    "ConcurrencyError",
    "InMemorySnapshotStore",
    "StoredEvent",
]
