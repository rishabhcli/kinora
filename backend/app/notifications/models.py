"""Core value types for the notifications platform.

Pydantic models so they serialize cleanly to JSON for the outbox / delivery rows
and the API. The two central nouns:

* :class:`Notification` — a *resolved intent to notify a recipient* on a channel.
  It carries the rendered (localized) message and a stable ``idempotency_key`` so
  the outbox can dedup re-emissions of the same logical event.
* :class:`DeliveryRecord` — the *tracked outcome* of attempting to deliver a
  notification, with status + attempt history (kinora.md §12 status tracking).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.notifications.events import DomainEvent


class Channel(StrEnum):
    """A delivery channel — the §5.6 surfaces plus durable out-of-band ones."""

    IN_APP = "in_app"
    EMAIL = "email"
    PUSH = "push"
    WEBHOOK = "webhook"


class NotificationPriority(IntEnum):
    """Ordering + quiet-hours bypass policy. Higher is more urgent."""

    LOW = 10
    NORMAL = 20
    HIGH = 30
    #: URGENT bypasses quiet hours + digest batching (delivered immediately).
    URGENT = 40

    @property
    def bypasses_quiet_hours(self) -> bool:
        return self >= NotificationPriority.URGENT

    @property
    def digestable(self) -> bool:
        """Whether this priority is eligible for digest batching (urgent never is)."""
        return self < NotificationPriority.URGENT


class DeliveryStatus(StrEnum):
    """The lifecycle of a delivery attempt set (kinora.md §12 status tracking)."""

    PENDING = "pending"
    #: Held back by quiet hours / digest; will be released later.
    DEFERRED = "deferred"
    #: Suppressed by user preference (opted out) — terminal, not a failure.
    SUPPRESSED = "suppressed"
    SENDING = "sending"
    DELIVERED = "delivered"
    RETRYING = "retrying"
    FAILED = "failed"
    #: Gave up after the retry cap — moved to the dead-letter store.
    DEADLETTERED = "deadlettered"

    @property
    def is_terminal(self) -> bool:
        return self in {
            DeliveryStatus.DELIVERED,
            DeliveryStatus.SUPPRESSED,
            DeliveryStatus.DEADLETTERED,
        }


class Recipient(BaseModel):
    """Who a notification is for + the per-channel addresses to reach them."""

    user_id: str
    email: str | None = None
    push_token: str | None = None
    locale: str = "en"

    def address_for(self, channel: Channel) -> str | None:
        """The recipient's address on ``channel`` (``None`` if not reachable there)."""
        if channel is Channel.EMAIL:
            return self.email
        if channel is Channel.PUSH:
            return self.push_token
        if channel is Channel.IN_APP:
            return self.user_id
        # Webhook addresses live on the endpoint, not the recipient.
        return None


class RenderedMessage(BaseModel):
    """A localized, interpolated message ready to hand to a transport."""

    subject: str
    body: str
    locale: str = "en"
    #: Optional channel-specific extras (HTML body, push title, action url …).
    extra: dict[str, Any] = Field(default_factory=dict)


class Notification(BaseModel):
    """A resolved intent to notify one recipient on one channel.

    The dispatcher receives a list of these (one per opted-in channel) from the
    EventRouter, then gates, renders, outboxes, and delivers each.
    """

    id: str
    event: DomainEvent
    channel: Channel
    recipient: Recipient
    priority: NotificationPriority = NotificationPriority.NORMAL
    #: Stable across re-emissions of the same logical event → outbox dedup.
    idempotency_key: str
    #: Template variables (book title, remaining seconds, conflict id, …).
    data: dict[str, Any] = Field(default_factory=dict)
    book_id: str | None = None
    session_id: str | None = None
    #: Filled by the dispatcher after rendering; not required at construction.
    message: RenderedMessage | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def outbox_key(self) -> str:
        """The outbox dedup key — one logical delivery per (event, channel, recipient)."""
        return f"{self.idempotency_key}:{self.channel.value}:{self.recipient.user_id}"


class DeliveryRecord(BaseModel):
    """The tracked outcome of delivering a notification (status + history)."""

    notification_id: str
    channel: Channel
    user_id: str
    status: DeliveryStatus = DeliveryStatus.PENDING
    attempts: int = 0
    last_error: str | None = None
    #: Set when DEFERRED — when the dispatcher should re-attempt.
    not_before: datetime | None = None
    delivered_at: datetime | None = None
    provider_message_id: str | None = None
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


__all__ = [
    "Channel",
    "DeliveryRecord",
    "DeliveryStatus",
    "Notification",
    "NotificationPriority",
    "Recipient",
    "RenderedMessage",
]
