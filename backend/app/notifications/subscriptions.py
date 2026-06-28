"""Event subscriptions — map a domain event onto the notifications to emit.

The :class:`EventRouter` is the bridge between *what happened* (a
:class:`~app.notifications.events.DomainEventEnvelope`) and *who to notify and
how* (a fan-out of :class:`~app.notifications.models.Notification`, one per
opted-in channel). It is pure routing: it consults the recipient's preferences to
decide the channel set and assigns each event a default priority; it does **not**
deliver (that's the dispatcher) and does **not** render templates (the dispatcher
renders just before send, so the locale is resolved per-recipient).

Priority defaults encode the §5/§7 product intent: a surfaced continuity conflict
is URGENT (it blocks generation and must bypass quiet hours); budget-low and
book-failed are HIGH; routine render/regen completions are LOW (prime digest
candidates).
"""

from __future__ import annotations

import uuid

from app.notifications.events import DomainEvent, DomainEventEnvelope
from app.notifications.models import Notification, NotificationPriority, Recipient
from app.notifications.preferences import NotificationPreferences

#: Per-event default priority (the dispatcher uses this for quiet-hours / digest).
_PRIORITY: dict[DomainEvent, NotificationPriority] = {
    DomainEvent.BOOK_READY: NotificationPriority.NORMAL,
    DomainEvent.BOOK_FAILED: NotificationPriority.HIGH,
    DomainEvent.RENDER_DONE: NotificationPriority.LOW,
    DomainEvent.REGEN_DONE: NotificationPriority.NORMAL,
    DomainEvent.BUDGET_LOW: NotificationPriority.HIGH,
    DomainEvent.CONFLICT_SURFACED: NotificationPriority.URGENT,
    DomainEvent.RENDER_DEADLETTER: NotificationPriority.NORMAL,
    DomainEvent.DIGEST_READY: NotificationPriority.LOW,
}


def priority_for(event: DomainEvent) -> NotificationPriority:
    """The default notification priority for a domain event."""
    return _PRIORITY.get(event, NotificationPriority.NORMAL)


class EventRouter:
    """Fan a domain event out into per-channel notifications for a recipient."""

    def route(
        self,
        envelope: DomainEventEnvelope,
        *,
        recipient: Recipient,
        preferences: NotificationPreferences,
    ) -> list[Notification]:
        """Build one :class:`Notification` per channel the recipient opted into.

        Returns an empty list when the recipient has opted out of the event on
        every channel (a fully-suppressed event), which the dispatcher records as
        a suppressed delivery rather than an error.
        """
        priority = priority_for(envelope.event)
        channels = preferences.channels_for(envelope.event, priority=priority)
        if not channels:
            return []
        idem = envelope.idempotency_key()
        notifications: list[Notification] = []
        for channel in sorted(channels, key=lambda c: c.value):
            notifications.append(
                Notification(
                    id=f"ntf_{uuid.uuid4().hex[:16]}",
                    event=envelope.event,
                    channel=channel,
                    recipient=recipient,
                    priority=priority,
                    idempotency_key=idem,
                    data=envelope.data,
                    book_id=envelope.book_id,
                    session_id=envelope.session_id,
                )
            )
        return notifications


__all__ = ["EventRouter", "priority_for"]
