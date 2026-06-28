"""Build a DB-backed :class:`NotificationService` from a session factory.

The service holds long-lived store seams, but the repositories are per-session
(the project's unit-of-work convention). This module bridges the two with thin
*session-per-call* adapter stores: each store method opens a short-lived
committing unit of work, runs the repo, and returns. That keeps each persistence
op transactional without the service holding a session open across its lifetime.

This is what the composition root wires in production; tests can still construct
``NotificationService`` directly with the in-memory stores.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from app.notifications.backoff import RetryPolicy
from app.notifications.bridge import NotificationBridge
from app.notifications.deadletter import DeadLetter
from app.notifications.delivery import DeliveryRecord
from app.notifications.inapp import InAppNotification
from app.notifications.models import DeliveryStatus, Notification, Recipient
from app.notifications.outbox import OutboxEntry
from app.notifications.preferences import NotificationPreferences
from app.notifications.repository import (
    DeadLetterRepo,
    DeliveryTrackerRepo,
    InAppStoreRepo,
    NotificationOutboxRepo,
    NotificationPrefsRepo,
    WebhookEndpointRepo,
)
from app.notifications.service import NotificationService
from app.notifications.transports import (
    EmailTransport,
    PushTransport,
    WebhookTransport,
)
from app.notifications.webhooks import WebhookEndpoint

SessionFactory = Callable[[], AbstractAsyncContextManager[Any]]


class _SessionOutbox:
    """Session-per-call adapter over :class:`NotificationOutboxRepo`."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def claim(self, notification: Notification) -> OutboxEntry | None:
        async with self._sf() as db:
            return await NotificationOutboxRepo(db).claim(notification)

    async def get(self, key: str) -> OutboxEntry | None:
        async with self._sf() as db:
            return await NotificationOutboxRepo(db).get(key)

    async def update_status(
        self,
        key: str,
        status: DeliveryStatus,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        not_before: float | None = None,
    ) -> None:
        async with self._sf() as db:
            await NotificationOutboxRepo(db).update_status(
                key, status, attempts=attempts, last_error=last_error, not_before=not_before
            )

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[OutboxEntry]:
        async with self._sf() as db:
            return await NotificationOutboxRepo(db).list_for_user(user_id, limit=limit)


class _SessionTracker:
    """Session-per-call adapter over :class:`DeliveryTrackerRepo`."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def record(self, record: DeliveryRecord) -> None:
        async with self._sf() as db:
            await DeliveryTrackerRepo(db).record(record)

    async def get(self, notification_id: str) -> DeliveryRecord | None:
        async with self._sf() as db:
            return await DeliveryTrackerRepo(db).get(notification_id)

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        async with self._sf() as db:
            return await DeliveryTrackerRepo(db).list_for_user(user_id, limit=limit)


class _SessionDeadLetters:
    """Session-per-call adapter over :class:`DeadLetterRepo`."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def add(self, dead_letter: DeadLetter) -> None:
        async with self._sf() as db:
            await DeadLetterRepo(db).add(dead_letter)

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeadLetter]:
        async with self._sf() as db:
            return await DeadLetterRepo(db).list_for_user(user_id, limit=limit)

    async def count(self) -> int:
        async with self._sf() as db:
            return await DeadLetterRepo(db).count()


class _SessionInApp:
    """Session-per-call adapter over :class:`InAppStoreRepo`."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._sf = session_factory

    async def add(self, item: InAppNotification) -> None:
        async with self._sf() as db:
            await InAppStoreRepo(db).add(item)

    async def list_for_user(
        self, user_id: str, *, limit: int = 50, unread_only: bool = False
    ) -> list[InAppNotification]:
        async with self._sf() as db:
            return await InAppStoreRepo(db).list_for_user(
                user_id, limit=limit, unread_only=unread_only
            )

    async def mark_read(self, user_id: str, notification_id: str) -> bool:
        async with self._sf() as db:
            return await InAppStoreRepo(db).mark_read(user_id, notification_id)

    async def unread_count(self, user_id: str) -> int:
        async with self._sf() as db:
            return await InAppStoreRepo(db).unread_count(user_id)


def build_notification_service(
    session_factory: SessionFactory,
    *,
    settings: Any | None = None,
    email_transport: EmailTransport | None = None,
    push_transport: PushTransport | None = None,
    webhook_transport: WebhookTransport | None = None,
    log: Callable[..., None] = lambda *a, **k: None,
) -> NotificationService:
    """Construct a DB-backed :class:`NotificationService`.

    The outbox / tracker / dead-letter / in-app stores persist to Postgres
    (session-per-call); preferences + webhook endpoints are resolved on demand
    from their repos. Transports default to the logging no-op transports (no
    network, no credits) unless real ones are injected. The §12.1 retry policy is
    driven by ``settings`` when supplied.
    """

    async def endpoints_for(user_id: str) -> list[WebhookEndpoint]:
        async with session_factory() as db:
            return await WebhookEndpointRepo(db).list_for_user(user_id, active_only=True)

    async def preferences_for(user_id: str) -> NotificationPreferences:
        async with session_factory() as db:
            return await NotificationPrefsRepo(db).get_or_default(user_id)

    retry_policy: RetryPolicy | None = None
    if settings is not None:
        retry_policy = RetryPolicy(
            max_attempts=getattr(settings, "notify_retry_max_attempts", 5),
            base_s=getattr(settings, "notify_retry_base_s", 2.0),
            factor=getattr(settings, "notify_retry_factor", 4.0),
            max_delay_s=getattr(settings, "notify_retry_max_delay_s", 300.0),
        )

    return NotificationService(
        outbox=_SessionOutbox(session_factory),
        tracker=_SessionTracker(session_factory),
        dead_letters=_SessionDeadLetters(session_factory),
        inapp_store=_SessionInApp(session_factory),
        email_transport=email_transport,
        push_transport=push_transport,
        webhook_transport=webhook_transport,
        endpoints_for=endpoints_for,
        preferences_for=preferences_for,
        retry_policy=retry_policy,
        log=log,
    )


def build_notification_bridge(
    redis: Any,
    service: NotificationService,
    session_factory: SessionFactory,
    *,
    log: Callable[..., None] = lambda *a, **k: None,
) -> NotificationBridge:
    """Build the live-event → notification bridge with DB-backed recipient lookups.

    Recipients are resolved from durable ownership: a book's ``user_id`` and a
    session's owner. A missing/orphaned owner yields ``None`` (the bridge then
    skips that event), so the bridge fails closed rather than mis-delivering.
    """

    async def recipient_for_book(book_id: str) -> Recipient | None:
        from app.db.repositories.book import BookRepo
        from app.db.repositories.user import UserRepo

        async with session_factory() as db:
            book = await BookRepo(db).get(book_id)
            if book is None or not book.user_id:
                return None
            user = await UserRepo(db).get(book.user_id)
        if user is None:
            return None
        return Recipient(user_id=user.id, email=user.email)

    async def recipient_for_session(session_id: str) -> Recipient | None:
        from app.db.repositories.session import SessionRepo
        from app.db.repositories.user import UserRepo

        async with session_factory() as db:
            row = await SessionRepo(db).get(session_id)
            if row is None or not row.user_id:
                return None
            user = await UserRepo(db).get(row.user_id)
        if user is None:
            return None
        return Recipient(user_id=user.id, email=user.email)

    async def title_for_book(book_id: str) -> str | None:
        from app.db.repositories.book import BookRepo

        async with session_factory() as db:
            book = await BookRepo(db).get(book_id)
        return book.title if book is not None else None

    return NotificationBridge(
        redis,
        service,
        recipient_for_book=recipient_for_book,
        recipient_for_session=recipient_for_session,
        title_for_book=title_for_book,
        log=log,
    )


__all__ = ["build_notification_bridge", "build_notification_service"]
