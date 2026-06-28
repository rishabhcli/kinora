"""Integration tests for the notifications DB repositories (infra-gated).

These require a throwaway Postgres (``KINORA_TEST_DATABASE_URL`` + ``_REDIS_URL`` +
``_S3_ENDPOINT_URL``); they skip cleanly otherwise. They exercise the round-trip
mapping between the platform's value types and the ORM rows, the durable
idempotent outbox claim, delivery-status persistence, the dead-letter store, the
in-app inbox, and webhook-endpoint CRUD.
"""

from __future__ import annotations

import uuid

import pytest

from app.db.base import new_id
from app.db.repositories.user import UserRepo
from app.notifications.deadletter import DeadLetter
from app.notifications.delivery import new_record
from app.notifications.events import DomainEvent
from app.notifications.inapp import InAppNotification
from app.notifications.models import (
    Channel,
    DeliveryStatus,
    Notification,
    NotificationPriority,
    Recipient,
)
from app.notifications.preferences import NotificationPreferences, overnight_quiet
from app.notifications.repository import (
    DeadLetterRepo,
    DeliveryTrackerRepo,
    InAppStoreRepo,
    NotificationOutboxRepo,
    NotificationPrefsRepo,
    WebhookEndpointRepo,
)
from tests.conftest import requires_infra

pytestmark = [requires_infra, pytest.mark.asyncio]


async def _make_user(container: object) -> str:
    """Create a user row and return its id (FKs require a real user)."""
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        user = await UserRepo(db).create(
            email=f"{uuid.uuid4().hex[:10]}@example.com", hashed_password="x"
        )
        return user.id


def _notification(user_id: str, *, channel: Channel = Channel.EMAIL) -> Notification:
    return Notification(
        id=f"ntf_{uuid.uuid4().hex[:12]}",
        event=DomainEvent.BOOK_READY,
        channel=channel,
        recipient=Recipient(user_id=user_id, email="r@e.com"),
        idempotency_key=f"book_ready:{uuid.uuid4().hex[:8]}",
        data={"title": "Moby-Dick"},
    )


# --------------------------------------------------------------------------- #
# preferences
# --------------------------------------------------------------------------- #


async def test_prefs_upsert_round_trip(container: object) -> None:
    user_id = await _make_user(container)
    prefs = (
        NotificationPreferences.defaults(user_id)
        .model_copy(update={"quiet_hours": overnight_quiet("America/New_York")})
        .with_event_channels(DomainEvent.RENDER_DONE, frozenset({Channel.EMAIL}))
    )
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        await NotificationPrefsRepo(db).upsert(prefs)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        loaded = await NotificationPrefsRepo(db).get(user_id)
    assert loaded is not None
    assert loaded.quiet_hours is not None
    assert loaded.quiet_hours.tz_name == "America/New_York"
    assert loaded.matrix[DomainEvent.RENDER_DONE] == frozenset({Channel.EMAIL})


async def test_prefs_get_or_default(container: object) -> None:
    user_id = await _make_user(container)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        prefs = await NotificationPrefsRepo(db).get_or_default(user_id)
    assert prefs.user_id == user_id
    assert Channel.IN_APP in prefs.channels_for(DomainEvent.BOOK_READY)


# --------------------------------------------------------------------------- #
# outbox idempotency (durable)
# --------------------------------------------------------------------------- #


async def test_outbox_claim_is_idempotent(container: object) -> None:
    user_id = await _make_user(container)
    notification = _notification(user_id)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        first = await NotificationOutboxRepo(db).claim(notification)
    assert first is not None
    # A second claim of the same logical delivery is a no-op (unique constraint).
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        second = await NotificationOutboxRepo(db).claim(notification)
    assert second is None


async def test_outbox_status_and_deferred_due(container: object) -> None:
    user_id = await _make_user(container)
    notification = _notification(user_id)
    key = notification.outbox_key()
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        await NotificationOutboxRepo(db).claim(notification)
        await NotificationOutboxRepo(db).update_status(
            key, DeliveryStatus.DEFERRED, not_before=1.0
        )
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        due = await NotificationOutboxRepo(db).due_entries(now=10.0)
    assert any(e.key == key for e in due)


# --------------------------------------------------------------------------- #
# delivery tracker
# --------------------------------------------------------------------------- #


async def test_delivery_tracker_persists_status(container: object) -> None:
    user_id = await _make_user(container)
    notification = _notification(user_id)
    record = new_record(notification, status=DeliveryStatus.DELIVERED)
    record.attempts = 2
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        await DeliveryTrackerRepo(db).record(record)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        loaded = await DeliveryTrackerRepo(db).get(notification.id)
        listed = await DeliveryTrackerRepo(db).list_for_user(user_id)
    assert loaded is not None
    assert loaded.status is DeliveryStatus.DELIVERED
    assert loaded.attempts == 2
    assert len(listed) == 1


# --------------------------------------------------------------------------- #
# dead-letter store
# --------------------------------------------------------------------------- #


async def test_dead_letter_persists(container: object) -> None:
    user_id = await _make_user(container)
    notification = _notification(user_id)
    dl = DeadLetter.from_notification(
        notification, attempts=5, last_error="boom", dead_letter_id=new_id()
    )
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        await DeadLetterRepo(db).add(dl)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        items = await DeadLetterRepo(db).list_for_user(user_id)
        count = await DeadLetterRepo(db).count()
    assert len(items) == 1
    assert items[0].last_error == "boom"
    assert count == 1


# --------------------------------------------------------------------------- #
# in-app inbox
# --------------------------------------------------------------------------- #


async def test_inbox_add_list_mark_read(container: object) -> None:
    user_id = await _make_user(container)
    item = InAppNotification(
        id=new_id(),
        user_id=user_id,
        event=DomainEvent.BOOK_READY,
        subject="Ready",
        body="Your book is ready",
        priority=NotificationPriority.NORMAL,
    )
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        await InAppStoreRepo(db).add(item)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        listed = await InAppStoreRepo(db).list_for_user(user_id)
        unread = await InAppStoreRepo(db).unread_count(user_id)
    assert len(listed) == 1
    assert unread == 1
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        ok = await InAppStoreRepo(db).mark_read(user_id, item.id)
        unread_after = await InAppStoreRepo(db).unread_count(user_id)
    assert ok is True
    assert unread_after == 0


# --------------------------------------------------------------------------- #
# webhook endpoints
# --------------------------------------------------------------------------- #


async def test_webhook_endpoint_crud(container: object) -> None:
    user_id = await _make_user(container)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        endpoint = await WebhookEndpointRepo(db).create(
            user_id=user_id, url="https://example.invalid/hook", events=frozenset({"book_ready"})
        )
    assert endpoint.secret.startswith("whsec_")
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        listed = await WebhookEndpointRepo(db).list_for_user(user_id)
        assert len(listed) == 1
        await WebhookEndpointRepo(db).set_active(endpoint.id, active=False)
    async with container.session_factory() as db:  # type: ignore[attr-defined]
        active = await WebhookEndpointRepo(db).list_for_user(user_id, active_only=True)
        assert active == []
        deleted = await WebhookEndpointRepo(db).delete(endpoint.id)
        assert deleted is True
