"""ORM models for the notifications & webhooks platform.

Five additive tables, all chaining on the current Alembic head; they touch no
existing table (only FK *to* ``users``). Enum-valued columns are stored as plain
``VARCHAR`` carrying the lowercase ``StrEnum`` *values* defined in
:mod:`app.notifications` — kept as strings here (not a SA ``Enum``) because the
canonical enums live in the notifications package, not ``db.models.enums``, and
the platform must round-trip them by value.

* ``notification_preferences`` — one row per user: opt-in matrix, quiet hours,
  digest cadence (JSONB), master mute, locale.
* ``webhook_endpoints`` — registered outbound destinations (url + secret + the
  subscribed event set).
* ``notification_outbox`` — the idempotent outbox; ``idempotency_key`` is unique
  so a duplicate emission is a no-op (§12.1).
* ``notification_deliveries`` — per-notification delivery status + attempt count
  (§12 status tracking).
* ``notification_deadletters`` — give-ups after the retry cap (the §12.1 DLQ).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin


class NotificationPreference(StrIdMixin, TimestampMixin, Base):
    """A user's notification settings (one row per user)."""

    __tablename__ = "notification_preferences"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_notification_preferences_user_id"),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: Master switch — when False, everything except URGENT is suppressed.
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    #: Globally enabled channels (list of channel values).
    enabled_channels: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    #: Per-event opt-in matrix: ``{event_value: [channel_value, ...]}``.
    matrix: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Quiet window: ``{start, end, tz_name, enabled}`` or ``null``.
    quiet_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Digest cadence: ``{enabled, interval_minutes}``.
    digest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    locale: Mapped[str] = mapped_column(String(16), default="en", nullable=False)


class WebhookEndpointRow(StrIdMixin, TimestampMixin, Base):
    """A registered outbound webhook destination for a user."""

    __tablename__ = "webhook_endpoints"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: HMAC signing secret (``whsec_...``).
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    #: Subscribed event values (``["*"]`` = all).
    events: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(256), nullable=True)


class NotificationOutbox(StrIdMixin, TimestampMixin, Base):
    """The idempotent outbox — one row per logical delivery (§12.1)."""

    __tablename__ = "notification_outbox"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_notification_outbox_idempotency_key"),
    )

    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: For DEFERRED entries: the UTC instant they become eligible again.
    not_before: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    #: The serialized notification payload (so a deferred entry can be sent later).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


class NotificationDelivery(StrIdMixin, CreatedAtMixin, Base):
    """Per-notification delivery-status record (§12 status tracking)."""

    __tablename__ = "notification_deliveries"

    notification_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    delivered_at: Mapped[Any | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[Any] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class NotificationInbox(StrIdMixin, CreatedAtMixin, Base):
    """The durable in-app inbox item — the persistent counterpart to the SSE feed."""

    __tablename__ = "notification_inbox"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    subject: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    priority: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)


class NotificationDeadLetter(StrIdMixin, CreatedAtMixin, Base):
    """A notification that gave up after exhausting retries (the §12.1 DLQ)."""

    __tablename__ = "notification_deadletters"

    notification_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)


__all__ = [
    "NotificationDeadLetter",
    "NotificationDelivery",
    "NotificationInbox",
    "NotificationOutbox",
    "NotificationPreference",
    "WebhookEndpointRow",
]
