"""The read-model projection seam — the CQRS *query side*.

The write side (commands, aggregates, the store) is the source of truth. The
**read side** derives query-optimised *read models* by folding the committed
event stream. A :class:`Projection` is a pure ``handle(event, metadata)`` fold
into its own in-memory (or, in production, persisted) state; a
:class:`ProjectionManager` fans committed events out to every registered
projection and tracks each one's processed position so it can resume.

This module owns only the *seam* and a couple of **reference projections** that
are useful in their own right and prove the seam:

* :class:`ShotStatusProjection` — a per-shot status/budget read model the UI's
  shot-timeline (§5.4) and the §11.1 budget accounting read from;
* :class:`SessionListProjection` — a per-user list of live/ended sessions.

Projections are intentionally decoupled from the bus: composition decides whether
to drive them inline (subscribe to the bus's committed events) or asynchronously
(replay from the store's global position on a background worker). The manager
takes :class:`~app.eventsourcing.store.StoredEvent`-shaped input so it can run off
either source. Both styles are exercised in the tests.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.db.models.enums import RenderJobStatus  # noqa: F401  (kept for API parity)
from app.eventsourcing.domain.events import (
    DomainEvent,
    EventMetadata,
    EventRegistry,
    deserialise,
    registry,
)
from app.eventsourcing.domain.render_shot import (
    ShotAccepted,
    ShotDegraded,
    ShotPlanned,
    ShotRegenRequested,
    ShotRendered,
    ShotTransitioned,
)
from app.eventsourcing.domain.session import (
    SessionEnded,
    SessionStarted,
)
from app.eventsourcing.domain.upcasting import UpcasterRegistry, upcasters
from app.eventsourcing.store.protocol import StoredEvent
from app.render.states import RenderState


@runtime_checkable
class Projection(Protocol):
    """A read model that folds committed domain events into queryable state."""

    name: str

    def handle(self, event: DomainEvent, metadata: EventMetadata) -> None:
        """Fold one committed event into the read model (pure; unknown -> ignore)."""
        ...


@dataclass(slots=True)
class ProjectionManager:
    """Fans committed events out to registered projections and tracks position.

    Args:
        projections: the read models to drive.
        event_registry / upcaster_registry: used to deserialise
            :class:`StoredEvent` envelopes when driving from the store (the
            in-process path hands :class:`DomainEvent` objects straight through).
    """

    projections: list[Projection] = field(default_factory=list)
    event_registry: EventRegistry = registry
    upcaster_registry: UpcasterRegistry = upcasters
    last_position: int = 0

    def register(self, projection: Projection) -> None:
        self.projections.append(projection)

    def apply(self, event: DomainEvent, metadata: EventMetadata) -> None:
        """Drive every projection with one in-process domain event."""
        for projection in self.projections:
            projection.handle(event, metadata)

    def apply_many(self, events: Sequence[tuple[DomainEvent, EventMetadata]]) -> None:
        for event, metadata in events:
            self.apply(event, metadata)

    def project_stored(self, stored: Sequence[StoredEvent]) -> int:
        """Drive projections from raw :class:`StoredEvent`\\ s (the catch-up path).

        Deserialises each envelope (running upcasters), folds it into every
        projection, and advances :attr:`last_position` to the last
        ``global_position`` seen. Returns the number of events processed.
        """
        processed = 0
        for record in stored:
            event, metadata = deserialise(
                {
                    "type": record.event_type,
                    "version": record.event_version,
                    "data": record.payload,
                    "meta": record.metadata,
                },
                event_registry=self.event_registry,
                upcasters=self.upcaster_registry,
            )
            self.apply(event, metadata)
            processed += 1
            if record.global_position is not None:
                self.last_position = record.global_position
        return processed


# --------------------------------------------------------------------------- #
# Reference projection: per-shot status + budget (§5.4 timeline, §11.1 budget)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ShotReadModel:
    """The queryable view of one shot."""

    shot_id: str
    book_id: str = ""
    state: str = RenderState.PLANNED.value
    video_seconds_spent: float = 0.0
    clip_url: str = ""
    accepted: bool = False
    degraded: bool = False
    regen_count: int = 0


@dataclass(slots=True)
class ShotStatusProjection:
    """A per-shot read model + a running total of video-seconds spent (§11.1).

    The shot-timeline UI reads each shot's ``state``/``clip_url``/badges from here
    without replaying the write-side stream, and the budget panel reads
    :attr:`total_video_seconds` — the headline §11.1 metric source.
    """

    name: str = "shot_status"
    shots: dict[str, ShotReadModel] = field(default_factory=dict)
    total_video_seconds: float = 0.0

    def handle(self, event: DomainEvent, _metadata: EventMetadata) -> None:
        if isinstance(event, ShotPlanned):
            self.shots[event.shot_id] = ShotReadModel(shot_id=event.shot_id, book_id=event.book_id)
        elif isinstance(event, ShotTransitioned):
            shot = self.shots.get(event.shot_id)
            if shot is not None:
                shot.state = event.to_state
        elif isinstance(event, ShotRendered):
            shot = self.shots.get(event.shot_id)
            if shot is not None:
                shot.clip_url = event.clip_url
                if not event.from_cache:
                    shot.video_seconds_spent += event.video_seconds
                    self.total_video_seconds += event.video_seconds
        elif isinstance(event, ShotAccepted):
            shot = self.shots.get(event.shot_id)
            if shot is not None:
                shot.accepted = True
                shot.state = RenderState.ACCEPTED.value
                if event.clip_url:
                    shot.clip_url = event.clip_url
        elif isinstance(event, ShotDegraded):
            shot = self.shots.get(event.shot_id)
            if shot is not None:
                shot.degraded = True
                shot.state = RenderState.DEGRADED.value
        elif isinstance(event, ShotRegenRequested):
            shot = self.shots.get(event.shot_id)
            if shot is not None:
                shot.regen_count += 1
                shot.accepted = False
                shot.degraded = False
                shot.state = RenderState.PROMOTED.value

    def shots_for_book(self, book_id: str) -> list[ShotReadModel]:
        return [s for s in self.shots.values() if s.book_id == book_id]

    def accepted_count(self) -> int:
        return sum(1 for s in self.shots.values() if s.accepted)


# --------------------------------------------------------------------------- #
# Reference projection: per-user session list
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SessionReadModel:
    """The queryable view of one reading session."""

    session_id: str
    user_id: str
    book_id: str
    live: bool = True


@dataclass(slots=True)
class SessionListProjection:
    """A per-user index of sessions, live ones first (the shelf 'continue' row)."""

    name: str = "session_list"
    sessions: dict[str, SessionReadModel] = field(default_factory=dict)

    def handle(self, event: DomainEvent, _metadata: EventMetadata) -> None:
        if isinstance(event, SessionStarted):
            self.sessions[event.session_id] = SessionReadModel(
                session_id=event.session_id,
                user_id=event.user_id,
                book_id=event.book_id,
                live=True,
            )
        elif isinstance(event, SessionEnded):
            session = self.sessions.get(event.session_id)
            if session is not None:
                session.live = False

    def live_sessions_for_user(self, user_id: str) -> list[SessionReadModel]:
        return [s for s in self.sessions.values() if s.user_id == user_id and s.live]

    def sessions_for_user(self, user_id: str) -> list[SessionReadModel]:
        return [s for s in self.sessions.values() if s.user_id == user_id]


def make_projection_sink(manager: ProjectionManager) -> ProjectionSink:
    """Adapt a :class:`ProjectionManager` to a bus-friendly committed-events sink.

    Returns an async callable the command bus can call with each batch of freshly
    committed ``(event, metadata)`` pairs so read models update inline. Keeping it
    a separate adapter means the manager stays free of any bus dependency.
    """

    async def sink(
        events: Sequence[tuple[DomainEvent, EventMetadata]],
    ) -> None:
        manager.apply_many(events)

    return sink


class ProjectionSink(Protocol):
    """An async sink the bus can push committed ``(event, metadata)`` batches to."""

    async def __call__(self, events: Sequence[tuple[DomainEvent, EventMetadata]]) -> None: ...


def metadata_view(metadata: EventMetadata) -> Mapping[str, object]:
    """A read-only view of an event's provenance (for projections that index it)."""
    return metadata.to_dict()


__all__ = [
    "Projection",
    "ProjectionManager",
    "ProjectionSink",
    "SessionListProjection",
    "SessionReadModel",
    "ShotReadModel",
    "ShotStatusProjection",
    "make_projection_sink",
    "metadata_view",
]
