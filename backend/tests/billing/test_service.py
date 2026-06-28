"""End-to-end integration tests for BillingService (isolated Postgres + fake provider)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from app.billing.default_catalog import (
    FEATURE_DIRECTOR_MODE,
    FEATURE_VOICE_CLONE,
)
from app.billing.enums import (
    AuditEvent,
    InvoiceStatus,
    SubscriptionStatus,
    UsageMeter,
)
from app.billing.errors import EntitlementDeniedError, InvalidStateError
from app.billing.provider.fake import FakePaymentProvider
from app.billing.service import BillingConfig, BillingService
from tests.billing.conftest import SessionFactory, requires_billing_db

pytestmark = requires_billing_db

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider()


@pytest.fixture
def service(session_factory: SessionFactory, provider: FakePaymentProvider) -> BillingService:
    return BillingService(
        session_factory=session_factory,
        provider=provider,
        config=BillingConfig(dunning_retry_days=(1, 3)),
    )


async def _seeded(service: BillingService) -> None:
    created = await service.seed_catalog()
    assert created >= 4
    # Idempotent: a second seed creates nothing.
    assert await service.seed_catalog() == 0


async def test_seed_and_list_plans(service: BillingService) -> None:
    await _seeded(service)
    plans = await service.list_plans()
    codes = {p["code"] for p in plans}
    assert {"free", "starter", "pro", "studio"} <= codes


async def test_ensure_customer_idempotent(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    uid = await make_user()
    cid1 = await service.ensure_customer(user_id=uid, email="r@example.com")
    cid2 = await service.ensure_customer(user_id=uid, email="r@example.com")
    assert cid1 == cid2


async def test_create_subscription_starts_trial(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email=None)
    sub = await service.create_subscription(customer_id=cid, plan_code="pro", now=NOW)
    assert sub["status"] == SubscriptionStatus.TRIALING.value
    assert sub["trial_end"] is not None


async def test_no_trial_plan_active_immediately(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email=None)
    # Free has 0 trial days -> active immediately.
    sub = await service.create_subscription(customer_id=cid, plan_code="free", now=NOW)
    assert sub["status"] == SubscriptionStatus.ACTIVE.value


async def test_entitlements_gate_director_mode(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email=None)
    await service.create_subscription(customer_id=cid, plan_code="free", now=NOW)
    ent = await service.entitlements_for(cid)
    with pytest.raises(EntitlementDeniedError):
        ent.require_feature(FEATURE_DIRECTOR_MODE)

    # Upgrade context: a Pro subscriber gets director mode + voice clone.
    cid2 = await service.ensure_customer(user_id=await make_user(), email=None)
    await service.create_subscription(customer_id=cid2, plan_code="pro", now=NOW)
    ent2 = await service.entitlements_for(cid2)
    ent2.require_feature(FEATURE_DIRECTOR_MODE)  # no raise (trialing == active gate)
    ent2.require_feature(FEATURE_VOICE_CLONE)


async def test_record_usage_idempotent(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email=None)
    sub = await service.create_subscription(customer_id=cid, plan_code="pro", now=NOW)
    sub_id = sub["id"]
    assert await service.record_usage(
        meter=UsageMeter.RENDER_SECONDS,
        quantity=5.0,
        subscription_id=sub_id,
        occurred_at=NOW,
        idempotency_key="shot_1",
    )
    assert not await service.record_usage(
        meter=UsageMeter.RENDER_SECONDS,
        quantity=5.0,
        subscription_id=sub_id,
        occurred_at=NOW,
        idempotency_key="shot_1",
    )
    summary = await service.usage_summary(sub_id)
    assert summary.quantity(UsageMeter.RENDER_SECONDS) == 5.0


async def test_generate_invoice_and_autocharge(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email="r@example.com")
    sub = await service.create_subscription(customer_id=cid, plan_code="pro", now=NOW)
    sub_id = sub["id"]
    # Activate trial so there's a paid period.
    await service.activate_trial(subscription_id=sub_id, now=NOW)
    # Add render overage usage: 1200 included; 2000 over.
    await service.record_usage(
        meter=UsageMeter.RENDER_SECONDS,
        quantity=3200,
        subscription_id=sub_id,
        occurred_at=NOW + timedelta(days=1),
        idempotency_key="batch_1",
    )
    invoice = await service.generate_period_invoice(
        subscription_id=sub_id, now=NOW + timedelta(days=2)
    )
    # $29 flat + $38 overage = $67.00; auto-charged via fake -> PAID.
    assert invoice["subtotal_minor"] == 2900 + 3800
    assert invoice["status"] == InvoiceStatus.PAID.value
    assert invoice["amount_paid_minor"] == 6700


async def test_failed_payment_drives_dunning(
    service: BillingService, provider: FakePaymentProvider, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email="r@example.com")
    sub = await service.create_subscription(customer_id=cid, plan_code="starter", now=NOW)
    sub_id = sub["id"]
    await service.activate_trial(subscription_id=sub_id, now=NOW)
    # Force the first payment to fail (sticky).
    provider.set_failure(always=True)
    invoice = await service.generate_period_invoice(
        subscription_id=sub_id, now=NOW + timedelta(days=1)
    )
    # $9 flat; failed -> OPEN, retry scheduled, subscription past_due.
    assert invoice["status"] == InvoiceStatus.OPEN.value
    assert invoice["next_attempt_at"] is not None
    # Retry once more (still failing) -> exhausted (schedule has 2 retries: 1d,3d;
    # 3 total attempts). attempt 2:
    await service.attempt_payment(invoice_id=invoice["id"], now=NOW + timedelta(days=2))
    # attempt 3 -> exhausted -> uncollectible + unpaid.
    final = await service.attempt_payment(invoice_id=invoice["id"], now=NOW + timedelta(days=5))
    assert final["status"] == InvoiceStatus.UNCOLLECTIBLE.value
    # Audit trail records dunning exhaustion.
    trail = await service.audit_trail(subscription_id=sub_id)
    events = {e["event"] for e in trail}
    assert AuditEvent.DUNNING_EXHAUSTED.value in events
    assert AuditEvent.PAYMENT_FAILED.value in events


async def test_recovery_after_failure(
    service: BillingService, provider: FakePaymentProvider, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email="r@example.com")
    sub = await service.create_subscription(customer_id=cid, plan_code="starter", now=NOW)
    sub_id = sub["id"]
    await service.activate_trial(subscription_id=sub_id, now=NOW)
    provider.fail_next_payment()  # only the first attempt fails
    invoice = await service.generate_period_invoice(
        subscription_id=sub_id, now=NOW + timedelta(days=1)
    )
    assert invoice["status"] == InvoiceStatus.OPEN.value
    # Retry succeeds -> PAID, subscription active again.
    recovered = await service.attempt_payment(invoice_id=invoice["id"], now=NOW + timedelta(days=2))
    assert recovered["status"] == InvoiceStatus.PAID.value


async def test_upgrade_proration(service: BillingService, make_user: Callable[..., object]) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email="r@example.com")
    sub = await service.create_subscription(customer_id=cid, plan_code="starter", now=NOW)
    sub_id = sub["id"]
    await service.activate_trial(subscription_id=sub_id, now=NOW)
    # Mid-period (15 of 30 days) upgrade Starter $9 -> Pro $29.
    mid = NOW + timedelta(days=15)
    changed = await service.change_plan(subscription_id=sub_id, new_plan_code="pro", now=mid)
    # A proration invoice was created with net ~ +$10 (charge $14.50 - credit $4.50).
    assert "proration_invoice" in changed
    proration = changed["proration_invoice"]
    assert proration["subtotal_minor"] == 1000


async def test_cancel_at_period_end_and_immediate(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email=None)
    sub = await service.create_subscription(customer_id=cid, plan_code="pro", now=NOW)
    sub_id = sub["id"]
    at_end = await service.cancel_subscription(subscription_id=sub_id, at_period_end=True, now=NOW)
    assert at_end["cancel_at_period_end"] is True
    assert at_end["status"] != SubscriptionStatus.CANCELED.value
    immediate = await service.cancel_subscription(
        subscription_id=sub_id, at_period_end=False, now=NOW
    )
    assert immediate["status"] == SubscriptionStatus.CANCELED.value
    # Cancelling again is an error.
    with pytest.raises(InvalidStateError):
        await service.cancel_subscription(subscription_id=sub_id, at_period_end=False, now=NOW)


async def test_free_plan_zero_invoice_marked_paid(
    service: BillingService, make_user: Callable[..., object]
) -> None:
    await _seeded(service)
    cid = await service.ensure_customer(user_id=await make_user(), email=None)
    sub = await service.create_subscription(customer_id=cid, plan_code="free", now=NOW)
    invoice = await service.generate_period_invoice(subscription_id=sub["id"], now=NOW)
    assert invoice["total_minor"] == 0
    assert invoice["status"] == InvoiceStatus.PAID.value
