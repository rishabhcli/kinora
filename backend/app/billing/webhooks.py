"""Inbound provider-webhook handling — signed + idempotent.

A webhook POST is untrusted until verified. :class:`WebhookHandler`:

1. **verifies the signature** via the provider transport
   (:meth:`PaymentProvider.verify_and_parse_webhook`, HMAC-SHA256 + timestamp
   tolerance) — a bad/stale signature raises
   :class:`app.billing.errors.WebhookVerificationError`;
2. **dedups** on ``(provider, event_id)`` against ``billing_webhook_events`` so a
   provider's at-least-once delivery never double-applies an event (the same
   replay guard the render queue uses on ``shot_hash``);
3. **applies** the event to billing state (mark an invoice paid, fail a payment
   and advance dunning, sync a subscription status) inside the same transaction,
   then marks the stored event processed.

Each step writes an audit entry, so the webhook trail is fully reconstructable.
This is exercised end-to-end with the **fake** provider (which self-signs events)
— no real Stripe/network call ever happens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.billing.enums import (
    AuditEvent,
    InvoiceStatus,
    ProviderEventType,
    SubscriptionStatus,
)
from app.billing.errors import WebhookVerificationError
from app.billing.models import BillingWebhookEvent
from app.billing.provider.base import PaymentProvider, WebhookEvent
from app.billing.repositories import (
    AuditRepo,
    InvoiceRepo,
    SubscriptionRepo,
    WebhookRepo,
)
from app.billing.service import BillingService
from app.core.logging import get_logger

logger = get_logger("app.billing.webhooks")


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """The outcome of handling one inbound webhook."""

    event_id: str
    event_type: str
    status: str  # "applied" | "replayed" | "ignored"

    @property
    def applied(self) -> bool:
        return self.status == "applied"


@dataclass
class WebhookHandler:
    """Verify + dedup + apply inbound provider webhooks (idempotently)."""

    service: BillingService
    provider: PaymentProvider

    async def handle(self, *, payload: bytes, signature_header: str) -> WebhookResult:
        """Verify, dedup, and apply a raw webhook body.

        Raises :class:`WebhookVerificationError` on a bad signature (the route
        maps it to 400). A duplicate event is a no-op returning ``replayed``.
        """
        event = self.provider.verify_and_parse_webhook(
            payload=payload, signature_header=signature_header
        )
        if not event.id:
            raise WebhookVerificationError("webhook event missing id")

        async with self.service.session_factory() as db:
            webhook_repo = WebhookRepo(db)
            stored = BillingWebhookEvent(
                provider=self.provider.config.name,
                event_id=event.id,
                event_type=event.type,
                payload={"id": event.id, "type": event.type, "data": event.data},
                processed=False,
                received_at=datetime.now(tz=UTC),
            )
            is_new = await webhook_repo.record(stored)
            if not is_new:
                logger.info("billing.webhook.replayed", event_id=event.id, type=event.type)
                await self._audit(db, AuditEvent.WEBHOOK_REPLAYED, event)
                return WebhookResult(event.id, event.type, "replayed")

            await self._audit(db, AuditEvent.WEBHOOK_RECEIVED, event)
            applied = await self._apply(db, event)
            await webhook_repo.mark_processed(
                provider=self.provider.config.name,
                event_id=event.id,
                at=datetime.now(tz=UTC),
            )
            return WebhookResult(event.id, event.type, "applied" if applied else "ignored")

    async def _apply(self, db, event: WebhookEvent) -> bool:  # type: ignore[no-untyped-def]
        """Apply a recognized event to billing state; return whether it did."""
        etype = event.type
        if etype == ProviderEventType.PAYMENT_SUCCEEDED.value:
            return await self._mark_invoice_paid(db, event)
        if etype == ProviderEventType.PAYMENT_FAILED.value:
            return await self._mark_invoice_failed(db, event)
        if etype in (
            ProviderEventType.SUBSCRIPTION_UPDATED.value,
            ProviderEventType.SUBSCRIPTION_DELETED.value,
        ):
            return await self._sync_subscription(db, event)
        logger.info("billing.webhook.ignored", type=etype)
        return False

    async def _mark_invoice_paid(self, db, event: WebhookEvent) -> bool:  # type: ignore[no-untyped-def]
        invoice_id = str(event.data.get("invoice_id", ""))
        if not invoice_id:
            return False
        repo = InvoiceRepo(db)
        invoice = await repo.get(invoice_id)
        if invoice is None or invoice.status not in (InvoiceStatus.OPEN, InvoiceStatus.DRAFT):
            return False
        invoice.status = InvoiceStatus.PAID
        invoice.amount_paid_minor = invoice.total_minor
        invoice.paid_at = datetime.now(tz=UTC)
        invoice.next_attempt_at = None
        await db.flush()
        if invoice.subscription_id:
            sub = await SubscriptionRepo(db).get(invoice.subscription_id)
            if sub is not None and sub.status in (
                SubscriptionStatus.PAST_DUE,
                SubscriptionStatus.UNPAID,
                SubscriptionStatus.INCOMPLETE,
            ):
                sub.status = SubscriptionStatus.ACTIVE
                await db.flush()
        await self._audit(
            db,
            AuditEvent.INVOICE_PAID,
            event,
            invoice_id=invoice_id,
            subscription_id=invoice.subscription_id,
        )
        return True

    async def _mark_invoice_failed(self, db, event: WebhookEvent) -> bool:  # type: ignore[no-untyped-def]
        invoice_id = str(event.data.get("invoice_id", ""))
        if not invoice_id:
            return False
        invoice = await InvoiceRepo(db).get(invoice_id)
        if invoice is None or invoice.status is not InvoiceStatus.OPEN:
            return False
        if invoice.subscription_id:
            sub = await SubscriptionRepo(db).get(invoice.subscription_id)
            if sub is not None and sub.status is SubscriptionStatus.ACTIVE:
                sub.status = SubscriptionStatus.PAST_DUE
                await db.flush()
        await self._audit(
            db,
            AuditEvent.PAYMENT_FAILED,
            event,
            invoice_id=invoice_id,
            subscription_id=invoice.subscription_id,
        )
        return True

    async def _sync_subscription(self, db, event: WebhookEvent) -> bool:  # type: ignore[no-untyped-def]
        subscription_id = str(event.data.get("subscription_id", ""))
        if not subscription_id:
            return False
        sub = await SubscriptionRepo(db).get(subscription_id)
        if sub is None:
            return False
        if event.type == ProviderEventType.SUBSCRIPTION_DELETED.value:
            sub.status = SubscriptionStatus.CANCELED
            sub.canceled_at = datetime.now(tz=UTC)
        else:
            raw_status = str(event.data.get("status", ""))
            try:
                sub.status = SubscriptionStatus(raw_status)
            except ValueError:
                logger.warning("billing.webhook.unknown_status", status=raw_status)
                return False
        await db.flush()
        await self._audit(
            db, AuditEvent.SUBSCRIPTION_UPDATED, event, subscription_id=subscription_id
        )
        return True

    async def _audit(  # type: ignore[no-untyped-def]
        self,
        db,
        kind: AuditEvent,
        event: WebhookEvent,
        *,
        invoice_id: str | None = None,
        subscription_id: str | None = None,
    ) -> None:
        from app.billing.models import BillingAuditLog

        await AuditRepo(db).record(
            BillingAuditLog(
                event=kind.value,
                occurred_at=datetime.now(tz=UTC),
                actor="provider",
                invoice_id=invoice_id,
                subscription_id=subscription_id,
                detail={"webhook_event_id": event.id, "webhook_type": event.type},
            )
        )


__all__ = ["WebhookHandler", "WebhookResult"]
