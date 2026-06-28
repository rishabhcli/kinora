"""The idempotent outbox (kinora.md §12.1 idempotency, applied to notifications).

The transactional-outbox pattern: before a notification is dispatched, an outbox
row is *claimed* under a unique idempotency key. A second emission of the same
logical event (a duplicate domain event, a retried HTTP request, a redelivered
pub/sub message) finds the key already present and is a **no-op** — the reader is
never notified twice for one thing, exactly as a duplicate Scheduler event can
never double-spend the video budget.

The outbox is also where delivery *status* is tracked: each entry carries a
:class:`~app.notifications.models.DeliveryStatus` that the dispatcher advances
(pending → sending → delivered / retrying / deadlettered), so an operator (or the
API) can answer "what happened to my notification?".

Two impls behind the :class:`Outbox` protocol: an in-memory one (tests + default
seam) and a Redis/DB-backed one wired in the repository/composition layers.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from app.notifications.models import DeliveryStatus, Notification


class OutboxEntry(BaseModel):
    """One claimed unit of outbound work, keyed by an idempotency key."""

    key: str
    notification: Notification
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    #: For DEFERRED entries: the epoch second they become eligible again.
    not_before: float | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class Outbox(Protocol):
    """The idempotent outbox seam."""

    async def claim(self, notification: Notification) -> OutboxEntry | None:
        """Reserve an entry for ``notification``; ``None`` if the key already exists."""
        ...

    async def get(self, key: str) -> OutboxEntry | None: ...

    async def update_status(
        self,
        key: str,
        status: DeliveryStatus,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        not_before: float | None = None,
    ) -> None: ...

    async def list_for_user(
        self, user_id: str, *, limit: int = 100
    ) -> list[OutboxEntry]: ...


class InMemoryOutbox:
    """A process-local idempotent outbox (tests + the default seam).

    ``claim`` is the dedup gate: it returns ``None`` for a key it has already
    seen, so a duplicate emission short-circuits before any send happens.
    """

    def __init__(self) -> None:
        self._entries: dict[str, OutboxEntry] = {}

    async def claim(self, notification: Notification) -> OutboxEntry | None:
        key = notification.outbox_key()
        if key in self._entries:
            return None
        entry = OutboxEntry(key=key, notification=notification)
        self._entries[key] = entry
        return entry

    async def get(self, key: str) -> OutboxEntry | None:
        return self._entries.get(key)

    async def update_status(
        self,
        key: str,
        status: DeliveryStatus,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        not_before: float | None = None,
    ) -> None:
        entry = self._entries.get(key)
        if entry is None:
            return
        entry.status = status
        if attempts is not None:
            entry.attempts = attempts
        if last_error is not None:
            entry.last_error = last_error
        entry.not_before = not_before
        entry.updated_at = datetime.now(UTC)

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[OutboxEntry]:
        entries = [
            e for e in self._entries.values() if e.notification.recipient.user_id == user_id
        ]
        entries.sort(key=lambda e: e.created_at, reverse=True)
        return entries[:limit]

    async def due_entries(self, *, now: float) -> list[OutboxEntry]:
        """Deferred entries whose ``not_before`` has passed (for the release sweep)."""
        return [
            e
            for e in self._entries.values()
            if e.status is DeliveryStatus.DEFERRED
            and (e.not_before is None or e.not_before <= now)
        ]


__all__ = ["InMemoryOutbox", "Outbox", "OutboxEntry"]
