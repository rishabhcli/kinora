"""Decoding the domain envelope out of a stored :class:`RecordedEvent`.

Aggregates write events through :func:`app.eventsourcing.domain.events.serialise`,
which produces the envelope ``{"type", "version", "data", "meta"}``. The store
persists that whole envelope as the :class:`RecordedEvent.payload` and assigns the
ordering coordinates (``version`` within the stream, ``global_position`` over the
log). When a projection reads the log back via ``read_all`` it gets the raw
``RecordedEvent``; this module turns it into a :class:`ProjectionEvent` whose
``type`` is the domain discriminator (e.g. ``"ShotRendered"``) and whose ``data``
is the event's own fields — what handlers actually want to fold.

The decode is **total and defensive**: a payload that is *not* a domain envelope
(no ``type``/``data`` keys) is treated as a bare payload, with the discriminator
falling back to :attr:`RecordedEvent.event_type` and ``data`` being the payload
itself. This keeps the read side robust to non-domain or legacy events without
needing the domain ``EventRegistry`` on the read path (projections key off the
``type`` string + raw ``data`` dict, not reconstructed event objects).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.eventsourcing.store.contracts import RecordedEvent


@dataclass(frozen=True, slots=True)
class ProjectionEvent:
    """A read-side view of one stored event, with the domain envelope decoded.

    Equality / hash is by :attr:`event_id` alone (the idempotency key) so a
    re-delivered event compares equal to its first delivery.
    """

    #: The store's unique event-occurrence id — the per-projection dedupe key.
    event_id: str
    #: The stream the event belongs to (e.g. ``"render_shot-shot_42"``).
    stream_id: str
    #: 0-based position within the stream.
    stream_version: int
    #: Dense, 1-based, store-wide ordering position the runner checkpoints on.
    global_position: int
    #: The domain discriminator (envelope ``type``; falls back to ``event_type``).
    type: str
    #: The event's own fields (envelope ``data``; falls back to the raw payload).
    data: Mapping[str, Any] = field(default_factory=dict)
    #: The provenance block (envelope ``meta``: actor/correlation/causation).
    meta: Mapping[str, Any] = field(default_factory=dict)
    #: When the store recorded the append (transaction time, UTC).
    recorded_at: datetime | None = None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ProjectionEvent):
            return NotImplemented
        return self.event_id == other.event_id

    def __hash__(self) -> int:
        return hash(self.event_id)

    @property
    def actor(self) -> str | None:
        """The ``actor_id`` from the envelope ``meta`` (or ``None``)."""
        actor = self.meta.get("actor_id")
        return actor if isinstance(actor, str) else None

    @property
    def correlation_id(self) -> str | None:
        cid = self.meta.get("correlation_id")
        return cid if isinstance(cid, str) else None


def decode(recorded: RecordedEvent) -> ProjectionEvent:
    """Turn a stored :class:`RecordedEvent` into a decoded :class:`ProjectionEvent`."""
    payload = recorded.payload if isinstance(recorded.payload, Mapping) else {}
    is_envelope = "type" in payload and "data" in payload
    if is_envelope:
        domain_type = str(payload["type"])
        raw_data = payload.get("data", {})
        data: Mapping[str, Any] = dict(raw_data) if isinstance(raw_data, Mapping) else {}
        raw_meta = payload.get("meta", {})
        meta: Mapping[str, Any] = dict(raw_meta) if isinstance(raw_meta, Mapping) else {}
    else:
        domain_type = recorded.event_type
        data = dict(payload)
        meta = recorded.metadata.to_dict()
    return ProjectionEvent(
        event_id=recorded.event_id,
        stream_id=recorded.stream_id,
        stream_version=recorded.version,
        global_position=recorded.global_position,
        type=domain_type,
        data=data,
        meta=meta,
        recorded_at=recorded.recorded_at,
    )


__all__ = [
    "ProjectionEvent",
    "decode",
]
