"""Delivery-status tracking (kinora.md §12 observability).

Every send attempt updates a :class:`~app.notifications.models.DeliveryRecord` so
the platform can answer "what happened to this notification?" — delivered,
deferred for quiet hours, suppressed by preference, retrying, or dead-lettered.

The :class:`DeliveryTracker` protocol is the seam; the in-memory recorder serves
tests + the default. (The outbox already stores per-entry status; this tracker is
the *channel-attempt-level* record, finer-grained, and is what the API surfaces.)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from app.notifications.models import (
    Channel,
    DeliveryRecord,
    DeliveryStatus,
    Notification,
)


class DeliveryTracker(Protocol):
    """Record + query delivery status for notifications."""

    async def record(self, record: DeliveryRecord) -> None: ...

    async def get(self, notification_id: str) -> DeliveryRecord | None: ...

    async def list_for_user(
        self, user_id: str, *, limit: int = 100
    ) -> list[DeliveryRecord]: ...


class InMemoryDeliveryTracker:
    """A process-local delivery-status tracker (tests + the default seam)."""

    def __init__(self) -> None:
        self._records: dict[str, DeliveryRecord] = {}

    async def record(self, record: DeliveryRecord) -> None:
        record.touch()
        self._records[record.notification_id] = record

    async def get(self, notification_id: str) -> DeliveryRecord | None:
        return self._records.get(notification_id)

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        records = [r for r in self._records.values() if r.user_id == user_id]
        records.sort(key=lambda r: r.updated_at, reverse=True)
        return records[:limit]

    async def counts_by_status(self) -> dict[str, int]:
        """Aggregate status counts (for the §12.5 metrics panel)."""
        counts: dict[str, int] = {}
        for record in self._records.values():
            counts[record.status.value] = counts.get(record.status.value, 0) + 1
        return counts


def new_record(notification: Notification, *, status: DeliveryStatus) -> DeliveryRecord:
    """Build a fresh delivery record for ``notification`` in ``status``."""
    return DeliveryRecord(
        notification_id=notification.id,
        channel=notification.channel,
        user_id=notification.recipient.user_id,
        status=status,
    )


def update_record(
    record: DeliveryRecord,
    *,
    status: DeliveryStatus,
    attempts: int | None = None,
    last_error: str | None = None,
    not_before: datetime | None = None,
    provider_message_id: str | None = None,
) -> DeliveryRecord:
    """Mutate + return ``record`` with a new status/attempt snapshot."""
    record.status = status
    if attempts is not None:
        record.attempts = attempts
    if last_error is not None:
        record.last_error = last_error
    record.not_before = not_before
    if provider_message_id is not None:
        record.provider_message_id = provider_message_id
    if status is DeliveryStatus.DELIVERED:
        record.delivered_at = datetime.now(UTC)
    record.touch()
    return record


def channel_label(channel: Channel) -> str:
    """A human label for a channel (UI / logs)."""
    return {
        Channel.IN_APP: "In-app",
        Channel.EMAIL: "Email",
        Channel.PUSH: "Push",
        Channel.WEBHOOK: "Webhook",
    }.get(channel, channel.value)


__all__ = [
    "DeliveryTracker",
    "InMemoryDeliveryTracker",
    "channel_label",
    "new_record",
    "update_record",
]
