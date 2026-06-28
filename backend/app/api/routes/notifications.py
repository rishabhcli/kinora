"""Notification & webhook routes — preferences, in-app inbox, webhooks, status.

The durable, out-of-band counterpart to the live ``/sessions/{id}/events`` SSE
stream (kinora.md §5.6). Where that stream is ephemeral (gone if the workspace is
closed), these endpoints surface the **persistent** notification platform:

* ``GET/PUT /me/notification-preferences`` — the opt-in matrix + quiet hours +
  digest cadence the dispatcher honours.
* ``GET /me/notifications`` (+ ``unread`` count) / ``POST /me/notifications/{id}/read``
  — the in-app inbox.
* ``POST/GET/DELETE /me/webhooks`` (+ ``/test`` + enable/disable) — outbound
  HMAC-signed webhook endpoints for third-party integrations.
* ``GET /me/notifications/deliveries`` / ``GET /me/notifications/dead-letters`` —
  delivery-status + dead-letter visibility (§12).

Schemas are defined locally (this route owns its transport contracts) to keep the
shared ``app.api.schemas`` module untouched. All routes require auth; webhook
secrets are returned **only** at creation time (never re-read).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.api.deps import ContainerDep, CurrentUser, write_rate_limit
from app.api.errors import APIError
from app.core.logging import get_logger
from app.notifications.events import DomainEvent
from app.notifications.models import Channel, NotificationPriority, Recipient
from app.notifications.preferences import (
    DigestCadence,
    NotificationPreferences,
)
from app.notifications.quiet_hours import QuietHours
from app.notifications.repository import (
    NotificationPrefsRepo,
    WebhookEndpointRepo,
)

logger = get_logger("app.api.notifications")

router = APIRouter(tags=["notifications"])


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #


class QuietHoursView(BaseModel):
    """A quiet-hours window (HH:MM strings)."""

    start: str = Field(examples=["22:00"])
    end: str = Field(examples=["07:00"])
    tz_name: str = "UTC"
    enabled: bool = True


class PreferencesView(BaseModel):
    """The user's notification preferences as JSON."""

    enabled: bool
    enabled_channels: list[str]
    matrix: dict[str, list[str]]
    quiet_hours: QuietHoursView | None
    digest_enabled: bool
    digest_interval_minutes: int
    locale: str


class UpdatePreferencesRequest(BaseModel):
    """Patch a user's notification preferences (all fields optional)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    enabled_channels: list[str] | None = None
    matrix: dict[str, list[str]] | None = None
    quiet_hours: QuietHoursView | None = None
    clear_quiet_hours: bool = False
    digest_enabled: bool | None = None
    digest_interval_minutes: int | None = Field(default=None, ge=1, le=1440)
    locale: str | None = Field(default=None, min_length=2, max_length=16)

    @field_validator("enabled_channels")
    @classmethod
    def _check_channels(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return [_validate_channel(v) for v in value]

    @field_validator("matrix")
    @classmethod
    def _check_matrix(cls, value: dict[str, list[str]] | None) -> dict[str, list[str]] | None:
        if value is None:
            return None
        out: dict[str, list[str]] = {}
        for event, channels in value.items():
            _validate_event(event)
            out[event] = [_validate_channel(c) for c in channels]
        return out


class InboxItemView(BaseModel):
    """One in-app inbox item."""

    id: str
    event: str
    subject: str
    body: str
    priority: int
    book_id: str | None
    read: bool
    created_at: str


class InboxResponse(BaseModel):
    items: list[InboxItemView]
    unread: int


class CreateWebhookRequest(BaseModel):
    """Register an outbound webhook endpoint."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=8, max_length=2048)
    #: Subscribed event values, or ``["*"]`` for all.
    events: list[str] = Field(default_factory=lambda: ["*"])
    description: str | None = Field(default=None, max_length=256)

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        value = value.strip()
        if not (value.startswith("https://") or value.startswith("http://")):
            raise ValueError("webhook url must be http(s)")
        return value

    @field_validator("events")
    @classmethod
    def _check_events(cls, value: list[str]) -> list[str]:
        return [e if e == "*" else _validate_event(e) for e in value]


class WebhookView(BaseModel):
    """A registered webhook endpoint (secret omitted)."""

    id: str
    url: str
    events: list[str]
    active: bool


class CreatedWebhookView(WebhookView):
    """A just-created webhook — the **only** time the signing secret is returned."""

    secret: str


class ActionResponse(BaseModel):
    ok: bool


class DeliveryView(BaseModel):
    notification_id: str
    channel: str
    status: str
    attempts: int
    last_error: str | None
    provider_message_id: str | None


class DeadLetterView(BaseModel):
    id: str
    notification_id: str
    channel: str
    event: str
    attempts: int
    last_error: str | None


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _validate_channel(value: str) -> str:
    try:
        return Channel(value).value
    except ValueError as exc:
        raise ValueError(f"unknown channel {value!r}") from exc


def _validate_event(value: str) -> str:
    try:
        return DomainEvent(value).value
    except ValueError as exc:
        raise ValueError(f"unknown event {value!r}") from exc


def _quiet_view(window: QuietHours | None) -> QuietHoursView | None:
    if window is None:
        return None
    return QuietHoursView(
        start=window.start.strftime("%H:%M"),
        end=window.end.strftime("%H:%M"),
        tz_name=window.tz_name,
        enabled=window.enabled,
    )


def _quiet_from_view(view: QuietHoursView) -> QuietHours:
    from datetime import time

    try:
        start = time.fromisoformat(view.start if len(view.start) > 5 else view.start + ":00")
        end = time.fromisoformat(view.end if len(view.end) > 5 else view.end + ":00")
    except ValueError as exc:
        raise APIError("invalid_request", f"bad quiet-hours time: {exc}", status=422) from exc
    return QuietHours(start=start, end=end, tz_name=view.tz_name, enabled=view.enabled)


def _prefs_view(prefs: NotificationPreferences) -> PreferencesView:
    return PreferencesView(
        enabled=prefs.enabled,
        enabled_channels=sorted(c.value for c in prefs.enabled_channels),
        matrix={e.value: sorted(c.value for c in chans) for e, chans in prefs.matrix.items()},
        quiet_hours=_quiet_view(prefs.quiet_hours),
        digest_enabled=prefs.digest.enabled,
        digest_interval_minutes=prefs.digest.interval_minutes,
        locale=prefs.locale,
    )


# --------------------------------------------------------------------------- #
# Preferences
# --------------------------------------------------------------------------- #


@router.get("/me/notification-preferences", response_model=PreferencesView)
async def get_preferences(container: ContainerDep, user: CurrentUser) -> PreferencesView:
    """The signed-in user's notification preferences (defaults if never set)."""
    async with container.session_factory() as db:
        prefs = await NotificationPrefsRepo(db).get_or_default(user.id)
    return _prefs_view(prefs)


@router.put("/me/notification-preferences", response_model=PreferencesView)
async def update_preferences(
    body: UpdatePreferencesRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> PreferencesView:
    """Patch the user's notification preferences and persist them."""
    async with container.session_factory() as db:
        repo = NotificationPrefsRepo(db)
        prefs = await repo.get_or_default(user.id)
        prefs = _apply_pref_patch(prefs, body)
        saved = await repo.upsert(prefs)
    logger.info("notifications.prefs_updated", user_id=user.id)
    return _prefs_view(saved)


def _apply_pref_patch(
    prefs: NotificationPreferences, body: UpdatePreferencesRequest
) -> NotificationPreferences:
    update: dict[str, object] = {}
    if body.enabled is not None:
        update["enabled"] = body.enabled
    if body.enabled_channels is not None:
        update["enabled_channels"] = frozenset(Channel(c) for c in body.enabled_channels)
    if body.matrix is not None:
        update["matrix"] = {
            DomainEvent(e): frozenset(Channel(c) for c in chans)
            for e, chans in body.matrix.items()
        }
    if body.clear_quiet_hours:
        update["quiet_hours"] = None
    elif body.quiet_hours is not None:
        update["quiet_hours"] = _quiet_from_view(body.quiet_hours)
    if body.digest_enabled is not None or body.digest_interval_minutes is not None:
        update["digest"] = DigestCadence(
            enabled=body.digest_enabled
            if body.digest_enabled is not None
            else prefs.digest.enabled,
            interval_minutes=body.digest_interval_minutes
            if body.digest_interval_minutes is not None
            else prefs.digest.interval_minutes,
        )
    if body.locale is not None:
        update["locale"] = body.locale
    return prefs.model_copy(update=update)


# --------------------------------------------------------------------------- #
# In-app inbox
# --------------------------------------------------------------------------- #


@router.get("/me/notifications", response_model=InboxResponse)
async def list_notifications(
    container: ContainerDep,
    user: CurrentUser,
    unread_only: bool = False,
    limit: int = 50,
) -> InboxResponse:
    """The user's in-app inbox (durable counterpart to the SSE feed)."""
    limit = max(1, min(limit, 200))
    items = await container.notifications.list_inbox(
        user.id, limit=limit, unread_only=unread_only
    )
    unread = await container.notifications.unread_count(user.id)
    return InboxResponse(
        items=[
            InboxItemView(
                id=i.id,
                event=i.event.value,
                subject=i.subject,
                body=i.body,
                priority=int(i.priority),
                book_id=i.book_id,
                read=i.read,
                created_at=i.created_at.isoformat(),
            )
            for i in items
        ],
        unread=unread,
    )


@router.post("/me/notifications/{notification_id}/read", response_model=ActionResponse)
async def mark_notification_read(
    notification_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ActionResponse:
    """Mark one in-app inbox item read."""
    ok = await container.notifications.mark_read(user.id, notification_id)
    return ActionResponse(ok=ok)


# --------------------------------------------------------------------------- #
# Webhooks
# --------------------------------------------------------------------------- #


@router.post("/me/webhooks", response_model=CreatedWebhookView, status_code=201)
async def create_webhook(
    body: CreateWebhookRequest,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> CreatedWebhookView:
    """Register an outbound webhook; the signing secret is returned only here."""
    async with container.session_factory() as db:
        endpoint = await WebhookEndpointRepo(db).create(
            user_id=user.id,
            url=body.url,
            events=frozenset(body.events),
            description=body.description,
        )
    logger.info("notifications.webhook_created", user_id=user.id, endpoint=endpoint.id)
    return CreatedWebhookView(
        id=endpoint.id,
        url=endpoint.url,
        events=sorted(endpoint.events),
        active=endpoint.active,
        secret=endpoint.secret,
    )


@router.get("/me/webhooks", response_model=list[WebhookView])
async def list_webhooks(container: ContainerDep, user: CurrentUser) -> list[WebhookView]:
    """The user's registered webhook endpoints (secrets omitted)."""
    async with container.session_factory() as db:
        endpoints = await WebhookEndpointRepo(db).list_for_user(user.id)
    return [
        WebhookView(id=e.id, url=e.url, events=sorted(e.events), active=e.active)
        for e in endpoints
    ]


@router.post("/me/webhooks/{endpoint_id}/enable", response_model=ActionResponse)
async def enable_webhook(
    endpoint_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ActionResponse:
    """Re-activate a webhook endpoint."""
    return ActionResponse(ok=await _set_active(container, user.id, endpoint_id, active=True))


@router.post("/me/webhooks/{endpoint_id}/disable", response_model=ActionResponse)
async def disable_webhook(
    endpoint_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ActionResponse:
    """Deactivate a webhook endpoint (kept for history; receives nothing)."""
    return ActionResponse(ok=await _set_active(container, user.id, endpoint_id, active=False))


@router.delete("/me/webhooks/{endpoint_id}", response_model=ActionResponse)
async def delete_webhook(
    endpoint_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ActionResponse:
    """Delete a webhook endpoint."""
    async with container.session_factory() as db:
        repo = WebhookEndpointRepo(db)
        await _assert_webhook_owner(repo, user.id, endpoint_id)
        ok = await repo.delete(endpoint_id)
    return ActionResponse(ok=ok)


@router.post("/me/webhooks/{endpoint_id}/test", response_model=ActionResponse)
async def test_webhook(
    endpoint_id: str,
    container: ContainerDep,
    user: CurrentUser,
    _rl: Annotated[None, Depends(write_rate_limit)],
) -> ActionResponse:
    """Send a signed test ping to the endpoint through the real delivery engine."""
    async with container.session_factory() as db:
        repo = WebhookEndpointRepo(db)
        endpoint = await repo.get(endpoint_id)
    if endpoint is None or endpoint.user_id != user.id:
        raise APIError("webhook_not_found", "no such webhook for this user", status=404)
    # Route a RENDER_DONE test through the platform on the WEBHOOK channel only, so
    # the full sign → deliver → status path runs against the user's real endpoint.
    recipient = Recipient(user_id=user.id, email=user.email)
    prefs = NotificationPreferences.defaults(user.id).with_event_channels(
        DomainEvent.RENDER_DONE, frozenset({Channel.WEBHOOK})
    )
    result = await container.notifications.emit(
        DomainEvent.RENDER_DONE,
        recipient=recipient,
        data={"test": True, "title": "Test ping"},
        dedup_key=f"test:{endpoint_id}:{NotificationPriority.NORMAL.value}",
        preferences=prefs,
    )
    return ActionResponse(ok=result.any_delivered)


# --------------------------------------------------------------------------- #
# Status + dead-letters
# --------------------------------------------------------------------------- #


@router.get("/me/notifications/deliveries", response_model=list[DeliveryView])
async def list_deliveries(container: ContainerDep, user: CurrentUser) -> list[DeliveryView]:
    """Delivery-status records for the user's notifications (§12 tracking)."""
    records = await container.notifications.list_deliveries(user.id)
    return [
        DeliveryView(
            notification_id=r.notification_id,
            channel=r.channel.value,
            status=r.status.value,
            attempts=r.attempts,
            last_error=r.last_error,
            provider_message_id=r.provider_message_id,
        )
        for r in records
    ]


@router.get("/me/notifications/dead-letters", response_model=list[DeadLetterView])
async def list_dead_letters(container: ContainerDep, user: CurrentUser) -> list[DeadLetterView]:
    """Notifications that gave up after exhausting retries (the §12.1 DLQ)."""
    items = await container.notifications.list_dead_letters(user.id)
    return [
        DeadLetterView(
            id=d.id,
            notification_id=d.notification_id,
            channel=d.channel.value,
            event=d.event,
            attempts=d.attempts,
            last_error=d.last_error,
        )
        for d in items
    ]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _set_active(
    container: ContainerDep, user_id: str, endpoint_id: str, *, active: bool
) -> bool:
    async with container.session_factory() as db:
        repo = WebhookEndpointRepo(db)
        await _assert_webhook_owner(repo, user_id, endpoint_id)
        return await repo.set_active(endpoint_id, active=active)


async def _assert_webhook_owner(
    repo: WebhookEndpointRepo, user_id: str, endpoint_id: str
) -> None:
    endpoint = await repo.get(endpoint_id)
    if endpoint is None or endpoint.user_id != user_id:
        raise APIError("webhook_not_found", "no such webhook for this user", status=404)


__all__ = ["router"]
