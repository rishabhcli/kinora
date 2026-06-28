"""Integration tests for the billing repositories (isolated Postgres)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.billing.enums import (
    BillingInterval,
    InvoiceStatus,
    MeteredAggregation,
    PaymentStatus,
    PlanTier,
    PriceType,
    SubscriptionStatus,
    UsageMeter,
)
from app.billing.models import (
    BillingCustomer,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPaymentAttempt,
    BillingPlan,
    BillingPrice,
    BillingSubscription,
    BillingWebhookEvent,
)
from app.billing.repositories import (
    CustomerRepo,
    InvoiceRepo,
    PaymentAttemptRepo,
    PlanRepo,
    SubscriptionRepo,
    UsageRepo,
    WebhookRepo,
)
from tests.billing.conftest import SessionFactory, requires_billing_db

pytestmark = requires_billing_db

T = datetime(2026, 1, 5, tzinfo=UTC)


async def _seed_plan_price(sf: SessionFactory) -> tuple[str, str]:
    async with sf() as db:
        plan = await PlanRepo(db).create(
            BillingPlan(code="pro", name="Pro", tier=PlanTier.PRO, trial_days=14, active=True)
        )
        price = await PlanRepo(db).add_price(
            BillingPrice(
                plan_id=plan.id,
                type=PriceType.FLAT,
                interval=BillingInterval.MONTH,
                currency="USD",
                flat_amount_minor=2900,
            )
        )
        return plan.id, price.id


async def test_plan_crud(session_factory: SessionFactory) -> None:
    plan_id, _ = await _seed_plan_price(session_factory)
    async with session_factory() as db:
        plan = await PlanRepo(db).get(plan_id)
        assert plan is not None and plan.code == "pro"
        by_code = await PlanRepo(db).get_by_code("pro")
        assert by_code is not None and by_code.id == plan_id
        actives = await PlanRepo(db).list_active()
        assert any(p.id == plan_id for p in actives)
        prices = await PlanRepo(db).prices_for_plan(plan_id)
        assert len(prices) == 1


async def test_customer_and_subscription(session_factory: SessionFactory) -> None:
    plan_id, price_id = await _seed_plan_price(session_factory)
    async with session_factory() as db:
        cust = await CustomerRepo(db).create(
            BillingCustomer(email="r@example.com", provider="fake", default_currency="USD")
        )
        cid = cust.id
        sub = await SubscriptionRepo(db).create(
            BillingSubscription(
                customer_id=cid,
                plan_id=plan_id,
                price_id=price_id,
                status=SubscriptionStatus.ACTIVE,
                currency="USD",
                current_period_start=T,
                current_period_end=T + timedelta(days=30),
            )
        )
        sub_id = sub.id
    async with session_factory() as db:
        active = await SubscriptionRepo(db).active_for_customer(cid)
        assert active is not None and active.id == sub_id


async def test_usage_record_idempotent_and_aggregate(session_factory: SessionFactory) -> None:
    plan_id, price_id = await _seed_plan_price(session_factory)
    async with session_factory() as db:
        cust = await CustomerRepo(db).create(
            BillingCustomer(provider="fake", default_currency="USD")
        )
        sub = await SubscriptionRepo(db).create(
            BillingSubscription(
                customer_id=cust.id,
                plan_id=plan_id,
                price_id=price_id,
                status=SubscriptionStatus.ACTIVE,
                currency="USD",
            )
        )
        sub_id = sub.id

    async with session_factory() as db:
        repo = UsageRepo(db)
        assert await repo.record(
            meter=UsageMeter.RENDER_SECONDS,
            quantity=5.0,
            occurred_at=T,
            subscription_id=sub_id,
            idempotency_key="shot_1",
        )
        # Same key -> idempotent no-op (survives the transaction).
        assert not await repo.record(
            meter=UsageMeter.RENDER_SECONDS,
            quantity=5.0,
            occurred_at=T,
            subscription_id=sub_id,
            idempotency_key="shot_1",
        )
        await repo.record(
            meter=UsageMeter.RENDER_SECONDS,
            quantity=7.0,
            occurred_at=T + timedelta(days=1),
            subscription_id=sub_id,
            idempotency_key="shot_2",
        )

    async with session_factory() as db:
        total = await UsageRepo(db).aggregate(
            UsageMeter.RENDER_SECONDS,
            MeteredAggregation.SUM,
            subscription_id=sub_id,
            period_start=T - timedelta(days=1),
            period_end=T + timedelta(days=30),
        )
        assert total == 12.0
        assert await UsageRepo(db).count(subscription_id=sub_id) == 2


async def test_usage_aggregate_max_and_last(session_factory: SessionFactory) -> None:
    plan_id, price_id = await _seed_plan_price(session_factory)
    async with session_factory() as db:
        cust = await CustomerRepo(db).create(
            BillingCustomer(provider="fake", default_currency="USD")
        )
        sub = await SubscriptionRepo(db).create(
            BillingSubscription(
                customer_id=cust.id,
                plan_id=plan_id,
                price_id=price_id,
                status=SubscriptionStatus.ACTIVE,
                currency="USD",
            )
        )
        sub_id = sub.id
    async with session_factory() as db:
        repo = UsageRepo(db)
        for i, q in enumerate((3.0, 9.0, 4.0)):
            await repo.record(
                meter=UsageMeter.BOOKS_IMPORTED,
                quantity=q,
                occurred_at=T + timedelta(hours=i),
                subscription_id=sub_id,
            )
    async with session_factory() as db:
        repo = UsageRepo(db)
        assert (
            await repo.aggregate(
                UsageMeter.BOOKS_IMPORTED, MeteredAggregation.MAX, subscription_id=sub_id
            )
            == 9.0
        )
        assert (
            await repo.aggregate(
                UsageMeter.BOOKS_IMPORTED, MeteredAggregation.LAST, subscription_id=sub_id
            )
            == 4.0
        )


async def test_invoice_and_lines_and_sequence(session_factory: SessionFactory) -> None:
    plan_id, price_id = await _seed_plan_price(session_factory)
    async with session_factory() as db:
        cust = await CustomerRepo(db).create(
            BillingCustomer(provider="fake", default_currency="USD")
        )
        sub = await SubscriptionRepo(db).create(
            BillingSubscription(
                customer_id=cust.id,
                plan_id=plan_id,
                price_id=price_id,
                status=SubscriptionStatus.ACTIVE,
                currency="USD",
            )
        )
        repo = InvoiceRepo(db)
        seq = await repo.next_sequence()
        assert seq == 1
        invoice = await repo.create(
            BillingInvoice(
                subscription_id=sub.id,
                customer_id=cust.id,
                number="KIN-2026-000001",
                status=InvoiceStatus.OPEN,
                currency="USD",
                subtotal_minor=2900,
                total_minor=2900,
            ),
            [
                BillingInvoiceLine(
                    invoice_id="", description="Pro plan", amount_minor=2900, currency="USD"
                )
            ],
        )
        inv_id = invoice.id
    async with session_factory() as db:
        repo = InvoiceRepo(db)
        lines = await repo.lines_for(inv_id)
        assert len(lines) == 1 and lines[0].amount_minor == 2900
        opens = await repo.open_invoices()
        assert any(i.id == inv_id for i in opens)
        # Next sequence advances past the numbered invoice.
        assert await repo.next_sequence() == 2


async def test_payment_attempts_append_only(session_factory: SessionFactory) -> None:
    plan_id, price_id = await _seed_plan_price(session_factory)
    async with session_factory() as db:
        cust = await CustomerRepo(db).create(
            BillingCustomer(provider="fake", default_currency="USD")
        )
        sub = await SubscriptionRepo(db).create(
            BillingSubscription(
                customer_id=cust.id,
                plan_id=plan_id,
                price_id=price_id,
                status=SubscriptionStatus.ACTIVE,
                currency="USD",
            )
        )
        invoice = await InvoiceRepo(db).create(
            BillingInvoice(
                subscription_id=sub.id,
                customer_id=cust.id,
                status=InvoiceStatus.OPEN,
                currency="USD",
                subtotal_minor=2900,
                total_minor=2900,
            ),
            [],
        )
        inv_id = invoice.id
    async with session_factory() as db:
        repo = PaymentAttemptRepo(db)
        await repo.record(
            BillingPaymentAttempt(
                invoice_id=inv_id,
                attempt_number=1,
                status=PaymentStatus.FAILED,
                amount_minor=2900,
                currency="USD",
                attempted_at=T,
                failure_code="card_declined",
            )
        )
        await repo.record(
            BillingPaymentAttempt(
                invoice_id=inv_id,
                attempt_number=2,
                status=PaymentStatus.SUCCEEDED,
                amount_minor=2900,
                currency="USD",
                attempted_at=T + timedelta(days=1),
            )
        )
    async with session_factory() as db:
        repo = PaymentAttemptRepo(db)
        attempts = await repo.for_invoice(inv_id)
        assert [a.attempt_number for a in attempts] == [1, 2]
        assert await repo.attempt_count(inv_id) == 2


async def test_webhook_idempotency(session_factory: SessionFactory) -> None:
    async with session_factory() as db:
        repo = WebhookRepo(db)
        assert not await repo.already_seen(provider="fake", event_id="evt_1")
        assert await repo.record(
            BillingWebhookEvent(
                provider="fake",
                event_id="evt_1",
                event_type="invoice.payment_succeeded",
                processed=False,
                received_at=T,
            )
        )
    async with session_factory() as db:
        repo = WebhookRepo(db)
        assert await repo.already_seen(provider="fake", event_id="evt_1")
        # Replay: same (provider, event_id) -> record returns False.
        assert not await repo.record(
            BillingWebhookEvent(
                provider="fake",
                event_id="evt_1",
                event_type="invoice.payment_succeeded",
                processed=False,
                received_at=T,
            )
        )
        await repo.mark_processed(provider="fake", event_id="evt_1", at=T)
