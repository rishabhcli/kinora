"""Kinora notifications & webhooks platform.

The durable, out-of-band counterpart to the live §5.6 event bus: it maps domain
events (book ready, render done, budget low, conflict surfaced) onto templated,
localized notifications, gates them by user preferences / quiet hours / digest
cadence, and delivers them across pluggable channels (in-app / email / push /
outbound HMAC-signed webhook) with §12-grade reliability — an idempotent outbox,
exponential-backoff retries, circuit breaking, a dead-letter store, and
delivery-status tracking.

:class:`~app.notifications.service.NotificationService` is the facade; everything
under it is an injectable seam with an in-memory default so the platform runs
with zero infrastructure and zero credits, and tests inject fakes.
"""

from __future__ import annotations

from app.notifications.events import DomainEvent, DomainEventEnvelope
from app.notifications.models import (
    Channel,
    DeliveryStatus,
    Notification,
    NotificationPriority,
    Recipient,
)
from app.notifications.preferences import NotificationPreferences
from app.notifications.service import NotificationService, NotifyResult

__all__ = [
    "Channel",
    "DeliveryStatus",
    "DomainEvent",
    "DomainEventEnvelope",
    "Notification",
    "NotificationPreferences",
    "NotificationPriority",
    "NotificationService",
    "NotifyResult",
    "Recipient",
]
