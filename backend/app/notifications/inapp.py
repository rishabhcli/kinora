"""The in-app notification inbox store.

The ``in_app`` channel doesn't go over a transport — it *persists* a notification
to a per-user inbox the desktop app reads (and the §5.4 live feed can mirror).
This is the durable counterpart to the ephemeral SSE feed: a notification a
reader missed while away is still here when they return.

Behind the :class:`InAppStore` protocol so the DB-backed impl (the repository)
can swap in; the in-memory one serves tests + local dev.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from pydantic import BaseModel, Field

from app.notifications.events import DomainEvent
from app.notifications.models import Notification, NotificationPriority


class InAppNotification(BaseModel):
    """A persisted in-app inbox item."""

    id: str
    user_id: str
    event: DomainEvent
    subject: str
    body: str
    priority: NotificationPriority = NotificationPriority.NORMAL
    book_id: str | None = None
    session_id: str | None = None
    data: dict[str, object] = Field(default_factory=dict)
    read: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def from_notification(cls, notification: Notification) -> InAppNotification:
        message = notification.message
        return cls(
            id=notification.id,
            user_id=notification.recipient.user_id,
            event=notification.event,
            subject=message.subject if message else notification.event.value,
            body=message.body if message else "",
            priority=notification.priority,
            book_id=notification.book_id,
            session_id=notification.session_id,
            data=notification.data,
        )


class InAppStore(Protocol):
    """Persist + query a user's in-app inbox."""

    async def add(self, item: InAppNotification) -> None: ...

    async def list_for_user(
        self, user_id: str, *, limit: int = 50, unread_only: bool = False
    ) -> list[InAppNotification]: ...

    async def mark_read(self, user_id: str, notification_id: str) -> bool: ...

    async def unread_count(self, user_id: str) -> int: ...


class InMemoryInAppStore:
    """A process-local in-app inbox (tests + the default seam)."""

    def __init__(self) -> None:
        self._items: dict[str, list[InAppNotification]] = {}

    async def add(self, item: InAppNotification) -> None:
        self._items.setdefault(item.user_id, []).append(item)

    async def list_for_user(
        self, user_id: str, *, limit: int = 50, unread_only: bool = False
    ) -> list[InAppNotification]:
        items = list(self._items.get(user_id, ()))
        if unread_only:
            items = [i for i in items if not i.read]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[:limit]

    async def mark_read(self, user_id: str, notification_id: str) -> bool:
        for item in self._items.get(user_id, ()):
            if item.id == notification_id and not item.read:
                item.read = True
                return True
        return False

    async def unread_count(self, user_id: str) -> int:
        return sum(1 for i in self._items.get(user_id, ()) if not i.read)


__all__ = ["InAppNotification", "InAppStore", "InMemoryInAppStore"]
