"""Facet B — the command + aggregate model (the CQRS *write side*).

Public surface, layered bottom-up:

* **events** (:mod:`.events`) — the :class:`DomainEvent` base, the envelope
  (de)serialisation, the type registry; **upcasting** (:mod:`.upcasting`) — the
  event-versioning + step-wise upcaster framework.
* **aggregate** (:mod:`.aggregate`) — :class:`AggregateRoot`: decide -> emit +
  rebuild-from-history; **identifiers** (:mod:`.identifiers`) — typed
  :class:`StreamId`.
* **commands** (:mod:`.commands`) + the concrete **commands_catalog** — the intent
  half of CQRS; **validators** — structural validation.
* **bus** (:mod:`.bus`) — the :class:`CommandBus`: route -> middleware -> retry ->
  stamp -> append -> sagas; **middleware** (:mod:`.middleware`) — validation, the
  auth seam, idempotency, logging; **concurrency** (:mod:`.concurrency`) — the
  optimistic-concurrency retry policy.
* **repository** (:mod:`.repository`) — the only bridge to the
  :class:`~app.eventsourcing.store.EventStore`.
* **saga** (:mod:`.saga`) + the concrete **sagas_catalog** — the saga-trigger seam
  (committed fact -> next command).
* the three aggregates — **session** (§5.2-§5.4, §9.6), **render_shot** (§9.7), and
  **canon** (§5.4, §8) — plus their **handlers** and the **wiring** that assembles
  a fully-configured bus over an injected store.

Import-safe end to end: no sockets, no DB, no event loop at import.
"""

from __future__ import annotations

from app.eventsourcing.domain.aggregate import AggregateRoot
from app.eventsourcing.domain.bus import CommandBus
from app.eventsourcing.domain.commands import Command, CommandResult
from app.eventsourcing.domain.concurrency import RetryPolicy, retry_on_conflict
from app.eventsourcing.domain.errors import (
    AggregateNotFound,
    AuthorizationError,
    CommandRejected,
    DomainError,
    InvariantViolation,
    ValidationError,
)
from app.eventsourcing.domain.events import (
    DomainEvent,
    EventMetadata,
    EventRegistry,
    register_events,
    registry,
    serialise,
)
from app.eventsourcing.domain.identifiers import StreamCategory, StreamId
from app.eventsourcing.domain.projection import (
    Projection,
    ProjectionManager,
    SessionListProjection,
    ShotStatusProjection,
)
from app.eventsourcing.domain.repository import Repository
from app.eventsourcing.domain.saga import SagaDispatcher, SagaTrigger
from app.eventsourcing.domain.snapshotting import SnapshotPolicy, Snapshotter
from app.eventsourcing.domain.upcasting import UpcasterRegistry, upcasters
from app.eventsourcing.domain.wiring import build_command_bus

__all__ = [
    "AggregateNotFound",
    "AggregateRoot",
    "AuthorizationError",
    "Command",
    "CommandRejected",
    "CommandResult",
    "CommandBus",
    "DomainError",
    "DomainEvent",
    "EventMetadata",
    "EventRegistry",
    "InvariantViolation",
    "Projection",
    "ProjectionManager",
    "Repository",
    "RetryPolicy",
    "SagaDispatcher",
    "SagaTrigger",
    "SessionListProjection",
    "ShotStatusProjection",
    "SnapshotPolicy",
    "Snapshotter",
    "StreamCategory",
    "StreamId",
    "UpcasterRegistry",
    "ValidationError",
    "build_command_bus",
    "register_events",
    "registry",
    "retry_on_conflict",
    "serialise",
    "upcasters",
]
