"""Integration tests for the idempotent, signed inbound-webhook handler."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.billing.enums import InvoiceStatus, ProviderEventType, SubscriptionStatus
from app.billing.errors import WebhookVerificationError
from app.billing.provider.fake import FakePaymentProvider
from app.billing.service import BillingConfig, BillingService
from app.billing.webhooks import WebhookHandler
from tests.billing.conftest import SessionFactory, requires_billing_db

pytestmark = requires_billing_db

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider()


@pytest.fixture
def service(session_factory: SessionFactory, provider: FakePaymentProvider) -> BillingService:
    # Don't auto-charge on finalize so the invoice stays OPEN for the webhook to settle.
    return BillingService(
        session_factory=session_factory,
        provider=provider,
        config=BillingConfig(auto_charge_on_finalize=False),
    )


@pytest.fixture
def handler(service: BillingService, provider: FakePaymentProvider) -> WebhookHandler:
    return WebhookHandler(service=service, provider=provider)


async def _open_invoice(
    service: BillingService, make_user: Callable[..., object]
) -> tuple[str, str]:
    await service.seed_catalog()
    cid = await service.ensure_customer(user_id=await make_user(), email="r@example.com")
    sub = await service.create_subscription(customer_id=cid, plan_code="pro", now=NOW)
    await service.activate_trial(subscription_id=sub["id"], now=NOW)
    invoice = await service.generate_period_invoice(
        subscription_id=sub["id"], now=NOW + timedelta(days=1)
    )
    assert invoice["status"] == InvoiceStatus.OPEN.value
    return sub["id"], invoice["id"]


async def test_payment_succeeded_webhook_marks_paid(
    handler: WebhookHandler,
    service: BillingService,
    provider: FakePaymentProvider,
    make_user: Callable[..., object],
) -> None:
    sub_id, invoice_id = await _open_invoice(service, make_user)
    payload, header = provider.emit_webhook(
        ProviderEventType.PAYMENT_SUCCEEDED.value,
        {"invoice_id": invoice_id},
        event_id="evt_paid_1",
    )
    result = await handler.handle(payload=payload, signature_header=header)
    assert result.applied
    # Invoice is now paid; audit trail recorded it.
    trail = await service.audit_trail(subscription_id=sub_id)
    assert any(e["event"] == "invoice_paid" for e in trail)


async def test_webhook_idempotent_replay(
    handler: WebhookHandler,
    service: BillingService,
    provider: FakePaymentProvider,
    make_user: Callable[..., object],
) -> None:
    _, invoice_id = await _open_invoice(service, make_user)
    payload, header = provider.emit_webhook(
        ProviderEventType.PAYMENT_SUCCEEDED.value,
        {"invoice_id": invoice_id},
        event_id="evt_replay",
    )
    first = await handler.handle(payload=payload, signature_header=header)
    assert first.status == "applied"
    # Re-delivering the same event id is a no-op.
    second = await handler.handle(payload=payload, signature_header=header)
    assert second.status == "replayed"


async def test_bad_signature_rejected(
    handler: WebhookHandler, provider: FakePaymentProvider
) -> None:
    payload, _ = provider.emit_webhook("invoice.payment_succeeded", {"invoice_id": "x"})
    with pytest.raises(WebhookVerificationError):
        await handler.handle(payload=payload, signature_header="t=1,v1=deadbeef")


async def test_payment_failed_webhook_sets_past_due(
    handler: WebhookHandler,
    service: BillingService,
    provider: FakePaymentProvider,
    make_user: Callable[..., object],
) -> None:
    sub_id, invoice_id = await _open_invoice(service, make_user)
    payload, header = provider.emit_webhook(
        ProviderEventType.PAYMENT_FAILED.value,
        {"invoice_id": invoice_id},
        event_id="evt_fail_1",
    )
    result = await handler.handle(payload=payload, signature_header=header)
    assert result.applied
    trail = await service.audit_trail(subscription_id=sub_id)
    assert any(e["event"] == "payment_failed" for e in trail)


async def test_subscription_deleted_webhook_cancels(
    handler: WebhookHandler,
    service: BillingService,
    provider: FakePaymentProvider,
    make_user: Callable[..., object],
) -> None:
    sub_id, _ = await _open_invoice(service, make_user)
    payload, header = provider.emit_webhook(
        ProviderEventType.SUBSCRIPTION_DELETED.value,
        {"subscription_id": sub_id},
        event_id="evt_del_1",
    )
    result = await handler.handle(payload=payload, signature_header=header)
    assert result.applied


async def test_unknown_event_ignored(
    handler: WebhookHandler, provider: FakePaymentProvider
) -> None:
    payload, header = provider.emit_webhook(
        "customer.discount.created", {"id": "x"}, event_id="evt_unknown"
    )
    result = await handler.handle(payload=payload, signature_header=header)
    assert result.status == "ignored"


async def test_subscription_updated_status_sync(
    handler: WebhookHandler,
    service: BillingService,
    provider: FakePaymentProvider,
    make_user: Callable[..., object],
) -> None:
    sub_id, _ = await _open_invoice(service, make_user)
    payload, header = provider.emit_webhook(
        ProviderEventType.SUBSCRIPTION_UPDATED.value,
        {"subscription_id": sub_id, "status": SubscriptionStatus.PAUSED.value},
        event_id="evt_upd_1",
    )
    result = await handler.handle(payload=payload, signature_header=header)
    assert result.applied
