"""DB-backed implementations of the notifications platform's store seams.

Each repository wraps an :class:`AsyncSession` (per the project's repository
convention: flush, never commit — the unit-of-work owns the transaction) and maps
between the platform's Pydantic value types and the ORM rows in
:mod:`app.db.models.notification`. They satisfy the same protocols the in-memory
stores do, so the composition root can swap them in without touching the
dispatcher / service.

The outbox repo's ``claim`` is the durable idempotency gate: it relies on the
``uq_notification_outbox_idempotency_key`` unique constraint — a concurrent
duplicate raises ``IntegrityError`` which we translate to "already claimed"
(``None``), exactly mirroring the render queue's check-and-set semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import time as _time

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.base import new_id
from app.db.models.notification import (
    NotificationDeadLetter as DeadLetterRow,
)
from app.db.models.notification import (
    NotificationDelivery as DeliveryRow,
)
from app.db.models.notification import (
    NotificationInbox as InboxRow,
)
from app.db.models.notification import (
    NotificationOutbox as OutboxRow,
)
from app.db.models.notification import (
    NotificationPreference as PreferenceRow,
)
from app.db.models.notification import (
    WebhookEndpointRow,
)
from app.db.repositories.base import BaseRepository
from app.notifications.deadletter import DeadLetter
from app.notifications.events import DomainEvent
from app.notifications.inapp import InAppNotification
from app.notifications.models import (
    Channel,
    DeliveryRecord,
    DeliveryStatus,
    Notification,
    NotificationPriority,
)
from app.notifications.outbox import OutboxEntry
from app.notifications.preferences import (
    DigestCadence,
    NotificationPreferences,
)
from app.notifications.quiet_hours import QuietHours
from app.notifications.webhooks import WebhookEndpoint, generate_webhook_secret

# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


class NotificationPrefsRepo(BaseRepository):
    """Load / upsert a user's :class:`NotificationPreferences`."""

    async def get(self, user_id: str) -> NotificationPreferences | None:
        row = await self._row(user_id)
        return _prefs_from_row(row) if row is not None else None

    async def get_or_default(self, user_id: str) -> NotificationPreferences:
        prefs = await self.get(user_id)
        return prefs if prefs is not None else NotificationPreferences.defaults(user_id)

    async def upsert(self, prefs: NotificationPreferences) -> NotificationPreferences:
        row = await self._row(prefs.user_id)
        if row is None:
            row = PreferenceRow(id=new_id(), user_id=prefs.user_id)
            self.session.add(row)
        row.enabled = prefs.enabled
        row.enabled_channels = sorted(c.value for c in prefs.enabled_channels)
        row.matrix = {
            e.value: sorted(c.value for c in chans) for e, chans in prefs.matrix.items()
        }
        row.quiet_hours = _quiet_to_json(prefs.quiet_hours)
        row.digest = {
            "enabled": prefs.digest.enabled,
            "interval_minutes": prefs.digest.interval_minutes,
        }
        row.locale = prefs.locale
        await self.session.flush()
        return _prefs_from_row(row)

    async def _row(self, user_id: str) -> PreferenceRow | None:
        stmt = select(PreferenceRow).where(PreferenceRow.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Webhook endpoints
# --------------------------------------------------------------------------- #


class WebhookEndpointRepo(BaseRepository):
    """CRUD over a user's registered webhook endpoints."""

    async def create(
        self,
        *,
        user_id: str,
        url: str,
        events: frozenset[str],
        secret: str | None = None,
        description: str | None = None,
        endpoint_id: str | None = None,
    ) -> WebhookEndpoint:
        row = WebhookEndpointRow(
            id=endpoint_id or new_id(),
            user_id=user_id,
            url=url,
            secret=secret or generate_webhook_secret(),
            events=sorted(events),
            active=True,
            description=description,
        )
        self.session.add(row)
        await self.session.flush()
        return _endpoint_from_row(row)

    async def list_for_user(
        self, user_id: str, *, active_only: bool = False
    ) -> list[WebhookEndpoint]:
        stmt = select(WebhookEndpointRow).where(WebhookEndpointRow.user_id == user_id)
        if active_only:
            stmt = stmt.where(WebhookEndpointRow.active.is_(True))
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_endpoint_from_row(r) for r in rows]

    async def get(self, endpoint_id: str) -> WebhookEndpoint | None:
        row = await self.session.get(WebhookEndpointRow, endpoint_id)
        return _endpoint_from_row(row) if row is not None else None

    async def set_active(self, endpoint_id: str, *, active: bool) -> bool:
        row = await self.session.get(WebhookEndpointRow, endpoint_id)
        if row is None:
            return False
        row.active = active
        await self.session.flush()
        return True

    async def delete(self, endpoint_id: str) -> bool:
        row = await self.session.get(WebhookEndpointRow, endpoint_id)
        if row is None:
            return False
        await self.session.delete(row)
        await self.session.flush()
        return True


# --------------------------------------------------------------------------- #
# Outbox (idempotent)
# --------------------------------------------------------------------------- #


class NotificationOutboxRepo(BaseRepository):
    """DB-backed idempotent outbox. ``claim`` relies on the unique constraint."""

    async def claim(self, notification: Notification) -> OutboxEntry | None:
        key = notification.outbox_key()
        existing = await self._by_key(key)
        if existing is not None:
            return None
        row = OutboxRow(
            id=new_id(),
            idempotency_key=key,
            user_id=notification.recipient.user_id,
            event=notification.event.value,
            channel=notification.channel.value,
            status=DeliveryStatus.PENDING.value,
            attempts=0,
            payload=notification.model_dump(mode="json"),
        )
        # Insert under a SAVEPOINT so a lost race (concurrent duplicate hitting the
        # unique constraint) rolls back *only this INSERT*, never the surrounding
        # unit of work. This keeps the convention "repos don't own the transaction"
        # while still giving the idempotent check-and-set its atomicity.
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            return None
        return _outbox_entry_from_row(row, notification)

    async def get(self, key: str) -> OutboxEntry | None:
        row = await self._by_key(key)
        return _outbox_entry_from_row(row) if row is not None else None

    async def update_status(
        self,
        key: str,
        status: DeliveryStatus,
        *,
        attempts: int | None = None,
        last_error: str | None = None,
        not_before: float | None = None,
    ) -> None:
        row = await self._by_key(key)
        if row is None:
            return
        row.status = status.value
        if attempts is not None:
            row.attempts = attempts
        if last_error is not None:
            row.last_error = last_error[:1000]
        row.not_before = (
            datetime.fromtimestamp(not_before, tz=UTC) if not_before is not None else None
        )
        await self.session.flush()

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[OutboxEntry]:
        stmt = (
            select(OutboxRow)
            .where(OutboxRow.user_id == user_id)
            .order_by(OutboxRow.created_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_outbox_entry_from_row(r) for r in rows]

    async def due_entries(self, *, now: float | None = None) -> list[OutboxEntry]:
        cutoff = datetime.fromtimestamp(now if now is not None else _time(), tz=UTC)
        stmt = select(OutboxRow).where(
            OutboxRow.status == DeliveryStatus.DEFERRED.value,
            (OutboxRow.not_before.is_(None)) | (OutboxRow.not_before <= cutoff),
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_outbox_entry_from_row(r) for r in rows]

    async def _by_key(self, key: str) -> OutboxRow | None:
        stmt = select(OutboxRow).where(OutboxRow.idempotency_key == key)
        return (await self.session.execute(stmt)).scalar_one_or_none()


# --------------------------------------------------------------------------- #
# Delivery-status tracker
# --------------------------------------------------------------------------- #


class DeliveryTrackerRepo(BaseRepository):
    """DB-backed delivery-status tracking."""

    async def record(self, record: DeliveryRecord) -> None:
        stmt = select(DeliveryRow).where(DeliveryRow.notification_id == record.notification_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is None:
            row = DeliveryRow(id=new_id(), notification_id=record.notification_id)
            self.session.add(row)
        row.user_id = record.user_id
        row.channel = record.channel.value
        row.status = record.status.value
        row.attempts = record.attempts
        row.last_error = record.last_error
        row.provider_message_id = record.provider_message_id
        row.delivered_at = record.delivered_at
        await self.session.flush()

    async def get(self, notification_id: str) -> DeliveryRecord | None:
        stmt = select(DeliveryRow).where(DeliveryRow.notification_id == notification_id)
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        return _delivery_from_row(row) if row is not None else None

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeliveryRecord]:
        stmt = (
            select(DeliveryRow)
            .where(DeliveryRow.user_id == user_id)
            .order_by(DeliveryRow.updated_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_delivery_from_row(r) for r in rows]


# --------------------------------------------------------------------------- #
# In-app inbox
# --------------------------------------------------------------------------- #


class InAppStoreRepo(BaseRepository):
    """DB-backed durable in-app inbox."""

    async def add(self, item: InAppNotification) -> None:
        row = InboxRow(
            id=item.id,
            user_id=item.user_id,
            event=item.event.value,
            subject=item.subject,
            body=item.body,
            priority=int(item.priority),
            book_id=item.book_id,
            session_id=item.session_id,
            data=item.data,
            read=item.read,
        )
        self.session.add(row)
        await self.session.flush()

    async def list_for_user(
        self, user_id: str, *, limit: int = 50, unread_only: bool = False
    ) -> list[InAppNotification]:
        stmt = select(InboxRow).where(InboxRow.user_id == user_id)
        if unread_only:
            stmt = stmt.where(InboxRow.read.is_(False))
        stmt = stmt.order_by(InboxRow.created_at.desc()).limit(limit)
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_inbox_from_row(r) for r in rows]

    async def mark_read(self, user_id: str, notification_id: str) -> bool:
        row = await self.session.get(InboxRow, notification_id)
        if row is None or row.user_id != user_id or row.read:
            return False
        row.read = True
        await self.session.flush()
        return True

    async def unread_count(self, user_id: str) -> int:
        from sqlalchemy import func

        stmt = (
            select(func.count())
            .select_from(InboxRow)
            .where(InboxRow.user_id == user_id, InboxRow.read.is_(False))
        )
        return int((await self.session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------- #
# Dead-letter store
# --------------------------------------------------------------------------- #


class DeadLetterRepo(BaseRepository):
    """DB-backed dead-letter store."""

    async def add(self, dead_letter: DeadLetter) -> None:
        row = DeadLetterRow(
            id=dead_letter.id,
            notification_id=dead_letter.notification_id,
            user_id=dead_letter.user_id,
            channel=dead_letter.channel.value,
            event=dead_letter.event,
            attempts=dead_letter.attempts,
            last_error=dead_letter.last_error,
            payload=dead_letter.payload,
        )
        self.session.add(row)
        await self.session.flush()

    async def list_for_user(self, user_id: str, *, limit: int = 100) -> list[DeadLetter]:
        stmt = (
            select(DeadLetterRow)
            .where(DeadLetterRow.user_id == user_id)
            .order_by(DeadLetterRow.created_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(stmt)).scalars().all()
        return [_dead_letter_from_row(r) for r in rows]

    async def count(self) -> int:
        from sqlalchemy import func

        stmt = select(func.count()).select_from(DeadLetterRow)
        return int((await self.session.execute(stmt)).scalar_one())


# --------------------------------------------------------------------------- #
# Row <-> value-type mapping
# --------------------------------------------------------------------------- #


def _prefs_from_row(row: PreferenceRow) -> NotificationPreferences:
    matrix: dict[DomainEvent, frozenset[Channel]] = {}
    for event_value, channels in (row.matrix or {}).items():
        try:
            event = DomainEvent(event_value)
        except ValueError:
            continue
        matrix[event] = frozenset(_channels(channels))
    return NotificationPreferences(
        user_id=row.user_id,
        enabled=row.enabled,
        enabled_channels=frozenset(_channels(row.enabled_channels or [])),
        matrix=matrix,
        quiet_hours=_quiet_from_json(row.quiet_hours),
        digest=DigestCadence(
            enabled=bool((row.digest or {}).get("enabled", False)),
            interval_minutes=int((row.digest or {}).get("interval_minutes", 60)),
        ),
        locale=row.locale,
    )


def _endpoint_from_row(row: WebhookEndpointRow) -> WebhookEndpoint:
    return WebhookEndpoint(
        id=row.id,
        user_id=row.user_id,
        url=row.url,
        secret=row.secret,
        events=frozenset(row.events or []),
        active=row.active,
    )


def _outbox_entry_from_row(
    row: OutboxRow, notification: Notification | None = None
) -> OutboxEntry:
    notif = notification or Notification.model_validate(row.payload)
    return OutboxEntry(
        key=row.idempotency_key,
        notification=notif,
        status=DeliveryStatus(row.status),
        attempts=row.attempts,
        last_error=row.last_error,
        not_before=row.not_before.timestamp() if row.not_before else None,
    )


def _delivery_from_row(row: DeliveryRow) -> DeliveryRecord:
    return DeliveryRecord(
        notification_id=row.notification_id,
        channel=Channel(row.channel),
        user_id=row.user_id,
        status=DeliveryStatus(row.status),
        attempts=row.attempts,
        last_error=row.last_error,
        delivered_at=row.delivered_at,
        provider_message_id=row.provider_message_id,
        updated_at=row.updated_at,
    )


def _inbox_from_row(row: InboxRow) -> InAppNotification:
    try:
        event = DomainEvent(row.event)
    except ValueError:
        event = DomainEvent.RENDER_DONE
    return InAppNotification(
        id=row.id,
        user_id=row.user_id,
        event=event,
        subject=row.subject,
        body=row.body,
        priority=NotificationPriority(row.priority),
        book_id=row.book_id,
        session_id=row.session_id,
        data=row.data or {},
        read=row.read,
        created_at=row.created_at,
    )


def _dead_letter_from_row(row: DeadLetterRow) -> DeadLetter:
    return DeadLetter(
        id=row.id,
        notification_id=row.notification_id,
        channel=Channel(row.channel),
        user_id=row.user_id,
        event=row.event,
        attempts=row.attempts,
        last_error=row.last_error,
        payload=row.payload or {},
        created_at=row.created_at,
    )


def _channels(values: list[str]) -> list[Channel]:
    out: list[Channel] = []
    for v in values:
        try:
            out.append(Channel(v))
        except ValueError:
            continue
    return out


def _quiet_to_json(window: QuietHours | None) -> dict[str, object] | None:
    if window is None:
        return None
    return {
        "start": window.start.isoformat(),
        "end": window.end.isoformat(),
        "tz_name": window.tz_name,
        "enabled": window.enabled,
    }


def _quiet_from_json(data: dict[str, object] | None) -> QuietHours | None:
    if not data:
        return None
    from datetime import time

    return QuietHours(
        start=time.fromisoformat(str(data["start"])),
        end=time.fromisoformat(str(data["end"])),
        tz_name=str(data.get("tz_name", "UTC")),
        enabled=bool(data.get("enabled", True)),
    )


__all__ = [
    "DeadLetterRepo",
    "DeliveryTrackerRepo",
    "InAppStoreRepo",
    "NotificationOutboxRepo",
    "NotificationPrefsRepo",
    "WebhookEndpointRepo",
]
