"""Domain events the notifications platform reacts to (kinora.md §5.6 / §12).

The live UI bus (Redis pub/sub → SSE/WS) carries the *ephemeral* §5.6 generation
events (``clip_ready``, ``buffer_state`` …). This module names the **durable**
domain events worth an out-of-band notification — the subset a reader cares about
even when the workspace is closed — and a small typed envelope carrying the data
a template needs to render the message.

The mapping from a raw §5.6 wire event onto a :class:`DomainEvent` lives in
:func:`from_session_event`, so the existing publishers stay untouched: a thin
adapter (the EventRouter, :mod:`app.notifications.subscriptions`) consumes the
same channel and emits notifications.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class DomainEvent(StrEnum):
    """A durable event that may produce an out-of-band notification.

    These intentionally do *not* mirror every §5.6 event — only the ones a
    reader is notified about when away from the live workspace.
    """

    #: Phase-A ingest finished; the book is ready to watch (§5.1).
    BOOK_READY = "book_ready"
    #: Ingest failed permanently (defect logged) — tell the reader to retry.
    BOOK_FAILED = "book_failed"
    #: A shot/scene render completed (§5.6 ``clip_ready`` / ``scene_stitched``).
    RENDER_DONE = "render_done"
    #: A Director-targeted regen finished (§5.6 ``regen_done``).
    REGEN_DONE = "regen_done"
    #: The video-second budget crossed the low watermark (§5.6 ``budget_low``).
    BUDGET_LOW = "budget_low"
    #: A continuity conflict needs the Director's decision (§7.2 ``conflict_choice``).
    CONFLICT_SURFACED = "conflict_surfaced"
    #: A render dead-lettered after exhausting retries (§12.1) — quality alert.
    RENDER_DEADLETTER = "render_deadletter"
    #: A rolled-up digest is ready to send (synthetic; produced by the digester).
    DIGEST_READY = "digest_ready"


#: The set of §5.6 wire ``event`` names that map onto a notifiable domain event.
_WIRE_TO_DOMAIN: dict[str, DomainEvent] = {
    "clip_ready": DomainEvent.RENDER_DONE,
    "scene_stitched": DomainEvent.RENDER_DONE,
    "regen_done": DomainEvent.REGEN_DONE,
    "budget_low": DomainEvent.BUDGET_LOW,
    "conflict_choice": DomainEvent.CONFLICT_SURFACED,
}


class DomainEventEnvelope(BaseModel):
    """A normalized domain event ready for routing into notifications.

    ``data`` carries template variables (e.g. ``{"title": "Moby-Dick"}``);
    ``user_id`` is the recipient when known (the EventRouter resolves it from the
    book/session when the raw event lacks it).
    """

    event: DomainEvent
    user_id: str | None = None
    book_id: str | None = None
    session_id: str | None = None
    #: A stable key for idempotent delivery (see :class:`Notification`).
    dedup_key: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def idempotency_key(self) -> str:
        """A stable idempotency key for this event occurrence.

        Prefers the explicit ``dedup_key``; otherwise composes one from the event
        kind + the most specific scope available so the same logical event
        produces the same key (and thus the same outbox row) on a replay.
        """
        if self.dedup_key:
            return f"{self.event.value}:{self.dedup_key}"
        scope = self.session_id or self.book_id or self.user_id or "global"
        return f"{self.event.value}:{scope}:{int(self.occurred_at.timestamp())}"


def from_session_event(
    message: dict[str, Any],
    *,
    user_id: str | None = None,
    book_id: str | None = None,
    session_id: str | None = None,
) -> DomainEventEnvelope | None:
    """Adapt a raw §5.6 wire event into a :class:`DomainEventEnvelope`.

    Returns ``None`` for wire events that are not notifiable (e.g. ``buffer_state``,
    ``keyframe_ready`` — those are live-UI only). The caller (EventRouter) supplies
    the ``user_id`` it resolved from the channel/book ownership.
    """
    wire = str(message.get("event", ""))
    domain = _WIRE_TO_DOMAIN.get(wire)
    if domain is None:
        return None
    data = {k: v for k, v in message.items() if k != "event"}
    return DomainEventEnvelope(
        event=domain,
        user_id=user_id,
        book_id=book_id or _opt_str(message.get("book_id")),
        session_id=session_id or _opt_str(message.get("session_id")),
        dedup_key=_dedup_for(domain, message),
        data=data,
    )


def _dedup_for(event: DomainEvent, message: dict[str, Any]) -> str | None:
    """Compose a per-occurrence dedup key from the wire payload when possible."""
    if event is DomainEvent.RENDER_DONE:
        sid = message.get("shot_id") or message.get("scene_id")
        return str(sid) if sid is not None else None
    if event is DomainEvent.REGEN_DONE:
        sid = message.get("shot_id")
        return str(sid) if sid is not None else None
    if event is DomainEvent.CONFLICT_SURFACED:
        cid = message.get("conflict_id")
        return str(cid) if cid is not None else None
    return None


def _opt_str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


__all__ = ["DomainEvent", "DomainEventEnvelope", "from_session_event"]
