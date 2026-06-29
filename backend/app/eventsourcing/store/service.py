"""The event-store DI seam: :class:`EventStoreFactory`.

The composition root constructs **one** factory (holding the process-wide event
serializer / type registry / snapshot strategy / outbox tuning) and callers do
``factory.store(session)`` / ``factory.outbox(session)`` / ``factory.inbox(session)``
per unit of work — exactly the moderation/notifications factory shape.

The factory is deliberately lazy and infra-free to construct: building it imports
no engine and opens no connection, so ``create_app()`` + ``/health`` stay cheap
(the composition-root rule). The Postgres-bound objects are only created when a
session is handed in.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.eventsourcing.store.contracts import EventSerializer
from app.eventsourcing.store.outbox import OutboxRelay
from app.eventsourcing.store.serialization import EventTypeRegistry, JsonEventSerializer
from app.eventsourcing.store.snapshot import SnapshotStrategy

if TYPE_CHECKING:
    from typing import TypeVar

    from app.core.config import Settings
    from app.eventsourcing.store.aggregate import Aggregate, AggregateRepository
    from app.eventsourcing.store.checkpoint import PostgresCheckpointStore
    from app.eventsourcing.store.contracts import EventStore, MessagePublisher
    from app.eventsourcing.store.inbox import PostgresInboxRepository
    from app.eventsourcing.store.postgres import (
        PostgresEventStore,
        PostgresOutboxRepository,
    )
    from app.eventsourcing.store.subscription import CatchUpSubscription, EventHandler

    _S = TypeVar("_S")

#: Built-in defaults; overridden from Settings when one is supplied.
DEFAULT_SNAPSHOT_EVERY = 50
DEFAULT_OUTBOX_BATCH = 100
DEFAULT_OUTBOX_MAX_ATTEMPTS = 8


class EventStoreFactory:
    """Builds session-bound event-store components (the DI seam).

    Holds the shared serializer (with its event-type registry) and the snapshot /
    outbox tuning, so every store handed out across the process shares one schema
    view and one cadence policy.
    """

    def __init__(
        self,
        *,
        serializer: EventSerializer | None = None,
        registry: EventTypeRegistry | None = None,
        snapshot_strategy: SnapshotStrategy | None = None,
        outbox_batch: int = DEFAULT_OUTBOX_BATCH,
        outbox_max_attempts: int = DEFAULT_OUTBOX_MAX_ATTEMPTS,
    ) -> None:
        if serializer is not None:
            self._serializer: EventSerializer = serializer
        else:
            self._serializer = JsonEventSerializer(registry or EventTypeRegistry())
        self._snapshot_strategy = snapshot_strategy or SnapshotStrategy(
            every=DEFAULT_SNAPSHOT_EVERY
        )
        self._outbox_batch = outbox_batch
        self._outbox_max_attempts = outbox_max_attempts

    # -- accessors ---------------------------------------------------------- #

    @property
    def serializer(self) -> EventSerializer:
        return self._serializer

    @property
    def registry(self) -> EventTypeRegistry:
        # The JsonEventSerializer exposes its registry; a custom serializer may not.
        reg = getattr(self._serializer, "registry", None)
        if isinstance(reg, EventTypeRegistry):
            return reg
        raise AttributeError("the configured serializer has no EventTypeRegistry")

    @property
    def snapshot_strategy(self) -> SnapshotStrategy:
        return self._snapshot_strategy

    # -- session-bound builders -------------------------------------------- #

    def store(self, session: AsyncSession) -> PostgresEventStore:
        from app.eventsourcing.store.postgres import PostgresEventStore

        return PostgresEventStore(session, serializer=self._serializer)

    def outbox_repository(self, session: AsyncSession) -> PostgresOutboxRepository:
        from app.eventsourcing.store.postgres import PostgresOutboxRepository

        return PostgresOutboxRepository(session)

    def inbox(self, session: AsyncSession) -> PostgresInboxRepository:
        from app.eventsourcing.store.inbox import PostgresInboxRepository

        return PostgresInboxRepository(session)

    def checkpoints(self, session: AsyncSession) -> PostgresCheckpointStore:
        from app.eventsourcing.store.checkpoint import PostgresCheckpointStore

        return PostgresCheckpointStore(session)

    def relay(
        self, session: AsyncSession, publisher: MessagePublisher
    ) -> OutboxRelay:
        """An :class:`OutboxRelay` over a session-bound outbox repo + ``publisher``."""
        return OutboxRelay(
            self.outbox_repository(session),
            publisher,
            batch_size=self._outbox_batch,
            max_attempts=self._outbox_max_attempts,
        )

    def aggregate_repository(
        self,
        aggregate: Aggregate[_S],
        session: AsyncSession,
    ) -> AggregateRepository[_S]:
        """A snapshot-accelerated :class:`AggregateRepository` for ``aggregate``.

        Wires the session-bound event store + snapshot store + the factory's
        configured :class:`SnapshotStrategy`, so the domain facet gets a turnkey
        event-sourced repository with the process-wide snapshot cadence.
        """
        from app.eventsourcing.store.aggregate import AggregateRepository

        store = self.store(session)
        return AggregateRepository(
            aggregate,
            store,
            snapshots=store,  # PostgresEventStore implements SnapshotStore too
            snapshot_strategy=self._snapshot_strategy,
        )

    def subscription(
        self,
        name: str,
        store: EventStore,
        session: AsyncSession,
        handler: EventHandler,
        *,
        batch_size: int = 100,
    ) -> CatchUpSubscription:
        """A :class:`CatchUpSubscription` with a Postgres-backed checkpoint store.

        ``store`` is passed in (rather than rebuilt) so the subscription reads the
        log through the same serializer/registry the writer used.
        """
        from app.eventsourcing.store.subscription import CatchUpSubscription

        return CatchUpSubscription(
            name,
            store,
            self.checkpoints(session),
            handler,
            batch_size=batch_size,
        )

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> EventStoreFactory:
        """Build a factory from :class:`Settings` (falls back to the defaults)."""
        if settings is None:
            from app.core.config import get_settings

            settings = get_settings()
        return cls(
            snapshot_strategy=SnapshotStrategy(
                every=getattr(settings, "es_snapshot_every", DEFAULT_SNAPSHOT_EVERY)
            ),
            outbox_batch=getattr(settings, "es_outbox_batch", DEFAULT_OUTBOX_BATCH),
            outbox_max_attempts=getattr(
                settings, "es_outbox_max_attempts", DEFAULT_OUTBOX_MAX_ATTEMPTS
            ),
        )


__all__ = [
    "DEFAULT_OUTBOX_BATCH",
    "DEFAULT_OUTBOX_MAX_ATTEMPTS",
    "DEFAULT_SNAPSHOT_EVERY",
    "EventStoreFactory",
]
