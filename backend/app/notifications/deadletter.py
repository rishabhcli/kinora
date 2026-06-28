"""Dead-letter store for notifications that exhausted their retries (§12.1).

Mirrors the render queue's DLQ philosophy: a delivery that fails past its retry
cap is *never* silently dropped — it lands in a durable dead-letter store with
the full failure context (channel, recipient, the rendered message, attempt
count, last error) so it can be inspected, alerted on, or replayed by an
operator. The pipeline never blocks on one bad recipient.

The store is an injectable seam: tests + local dev use the in-memory impl here;
the DB-backed impl lives in :mod:`app.notifications.repository`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from app.notifications.models import Channel, Notification


class DeadLetter(BaseModel):
    """A notification that gave up after exhausting retries."""

    id: str
    notification_id: str
    channel: Channel
    user_id: str
    event: str
    attempts: int
    last_error: str | None = None
    #: The notification's payload (serialized) so it can be replayed.
    payload: dict[str, object] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_notification(
        cls,
        notification: Notification,
        *,
        attempts: int,
        last_error: str | None,
        dead_letter_id: str,
    ) -> DeadLetter:
        """Build a dead-letter record capturing a notification's failed delivery."""
        return cls(
            id=dead_letter_id,
            notification_id=notification.id,
            channel=notification.channel,
            user_id=notification.recipient.user_id,
            event=notification.event.value,
            attempts=attempts,
            last_error=last_error,
            payload=notification.model_dump(mode="json"),
        )


class DeadLetterStore(Protocol):
    """Persist + query dead-lettered notifications."""

    async def add(self, dead_letter: DeadLetter) -> None: ...

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeadLetter]: ...

    async def count(self) -> int: ...


class InMemoryDeadLetterStore:
    """A non-durable dead-letter store (tests + the default seam)."""

    def __init__(self) -> None:
        self._items: list[DeadLetter] = []

    async def add(self, dead_letter: DeadLetter) -> None:
        self._items.append(dead_letter)

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeadLetter]:
        items = [d for d in self._items if d.user_id == user_id]
        items.sort(key=lambda d: d.created_at, reverse=True)
        return items[:limit]

    async def count(self) -> int:
        return len(self._items)

    async def all(self) -> list[DeadLetter]:
        """Every dead-letter, newest first (test/observability convenience)."""
        return sorted(self._items, key=lambda d: d.created_at, reverse=True)


__all__ = [
    "DeadLetter",
    "DeadLetterStore",
    "InMemoryDeadLetterStore",
]
