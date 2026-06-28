"""Async repositories over the billing tables.

Each repo wraps an :class:`AsyncSession` and owns the persistence + queries for
one aggregate, following the project convention: repos **flush** (to populate
defaults and surface constraint errors), never **commit** — the unit-of-work
boundary owns the transaction (``app.db.session.get_session`` /
``Container.session_factory``).

Money crosses the repo boundary as integer minor units + a currency string, the
same representation as the ORM columns; the service layer converts to/from
:class:`app.billing.money.Money`. The usage / payment-attempt / audit / webhook
repos are append-only and idempotent where the table is.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.billing.enums import (
    InvoiceStatus,
    MeteredAggregation,
    SubscriptionStatus,
    UsageMeter,
)
from app.billing.models import (
    BillingAuditLog,
    BillingCoupon,
    BillingCustomer,
    BillingInvoice,
    BillingInvoiceLine,
    BillingPaymentAttempt,
    BillingPlan,
    BillingPrice,
    BillingSubscription,
    BillingUsageRecord,
    BillingWebhookEvent,
)
from app.db.base import new_id
from app.db.repositories.base import BaseRepository


class PlanRepo(BaseRepository):
    """Plan + price catalog persistence."""

    async def get(self, plan_id: str) -> BillingPlan | None:
        return await self.session.get(BillingPlan, plan_id)

    async def get_by_code(self, code: str) -> BillingPlan | None:
        stmt = select(BillingPlan).where(BillingPlan.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active(self) -> list[BillingPlan]:
        stmt = select(BillingPlan).where(BillingPlan.active.is_(True)).order_by(BillingPlan.code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def create(self, plan: BillingPlan) -> BillingPlan:
        if plan.id is None:  # pragma: no cover - StrIdMixin defaults it
            plan.id = new_id()
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def add_price(self, price: BillingPrice) -> BillingPrice:
        self.session.add(price)
        await self.session.flush()
        return price

    async def get_price(self, price_id: str) -> BillingPrice | None:
        return await self.session.get(BillingPrice, price_id)

    async def prices_for_plan(self, plan_id: str) -> list[BillingPrice]:
        stmt = select(BillingPrice).where(BillingPrice.plan_id == plan_id)
        return list((await self.session.execute(stmt)).scalars().all())


class CustomerRepo(BaseRepository):
    """Billing-customer persistence."""

    async def get(self, customer_id: str) -> BillingCustomer | None:
        return await self.session.get(BillingCustomer, customer_id)

    async def get_by_user(self, user_id: str) -> BillingCustomer | None:
        stmt = select(BillingCustomer).where(BillingCustomer.user_id == user_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(self, customer: BillingCustomer) -> BillingCustomer:
        self.session.add(customer)
        await self.session.flush()
        return customer

    async def set_delinquent(self, customer_id: str, *, delinquent: bool) -> None:
        customer = await self.get(customer_id)
        if customer is not None:
            customer.delinquent = delinquent
            await self.session.flush()


class SubscriptionRepo(BaseRepository):
    """Subscription persistence + status queries."""

    async def get(self, subscription_id: str) -> BillingSubscription | None:
        return await self.session.get(BillingSubscription, subscription_id)

    async def create(self, sub: BillingSubscription) -> BillingSubscription:
        self.session.add(sub)
        await self.session.flush()
        return sub

    async def list_for_customer(self, customer_id: str) -> list[BillingSubscription]:
        stmt = (
            select(BillingSubscription)
            .where(BillingSubscription.customer_id == customer_id)
            .order_by(BillingSubscription.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def active_for_customer(self, customer_id: str) -> BillingSubscription | None:
        """The one current (trialing/active/past_due) subscription, if any."""
        stmt = (
            select(BillingSubscription)
            .where(
                BillingSubscription.customer_id == customer_id,
                BillingSubscription.status.in_(
                    (
                        SubscriptionStatus.TRIALING,
                        SubscriptionStatus.ACTIVE,
                        SubscriptionStatus.PAST_DUE,
                    )
                ),
            )
            .order_by(BillingSubscription.created_at.desc())
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def due_for_renewal(
        self, *, before: datetime, limit: int = 100
    ) -> list[BillingSubscription]:
        """Active subscriptions whose current period has ended (renewal scan)."""
        stmt = (
            select(BillingSubscription)
            .where(
                BillingSubscription.status.in_(
                    (SubscriptionStatus.ACTIVE, SubscriptionStatus.TRIALING)
                ),
                BillingSubscription.current_period_end.is_not(None),
                BillingSubscription.current_period_end <= before,
            )
            .limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class UsageRepo(BaseRepository):
    """Append-only metered usage records + windowed aggregation.

    Mirrors :class:`app.db.repositories.budget.BudgetRepo`: append immutable rows,
    aggregate with a windowed sum/max/last. Recording is idempotent on
    ``idempotency_key`` (a UNIQUE column); a duplicate key is swallowed via a
    SAVEPOINT so the surrounding transaction survives.
    """

    async def record(
        self,
        *,
        meter: UsageMeter,
        quantity: float,
        occurred_at: datetime,
        subscription_id: str | None = None,
        customer_id: str | None = None,
        book_id: str | None = None,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        note: str | None = None,
    ) -> bool:
        """Append a usage record; return False (no-op) if the key already exists."""
        record = BillingUsageRecord(
            id=new_id(),
            meter=meter,
            quantity=quantity,
            occurred_at=occurred_at,
            subscription_id=subscription_id,
            customer_id=customer_id,
            book_id=book_id,
            session_id=session_id,
            idempotency_key=idempotency_key,
            note=note,
        )
        if idempotency_key is None:
            self.session.add(record)
            await self.session.flush()
            return True
        # Idempotent path: a nested transaction lets a unique-violation roll back
        # only this insert without poisoning the outer unit of work.
        try:
            async with self.session.begin_nested():
                self.session.add(record)
                await self.session.flush()
        except IntegrityError:
            return False
        return True

    async def aggregate(
        self,
        meter: UsageMeter,
        aggregation: MeteredAggregation,
        *,
        subscription_id: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> float:
        """Windowed SUM/MAX/LAST of ``meter`` quantity over the scope."""
        clauses = [BillingUsageRecord.meter == meter]
        if subscription_id is not None:
            clauses.append(BillingUsageRecord.subscription_id == subscription_id)
        if period_start is not None:
            clauses.append(BillingUsageRecord.occurred_at >= period_start)
        if period_end is not None:
            clauses.append(BillingUsageRecord.occurred_at < period_end)

        if aggregation is MeteredAggregation.SUM:
            agg = func.coalesce(func.sum(BillingUsageRecord.quantity), 0.0)
            stmt = select(agg).where(*clauses)
            return float((await self.session.execute(stmt)).scalar_one())
        if aggregation is MeteredAggregation.MAX:
            agg = func.coalesce(func.max(BillingUsageRecord.quantity), 0.0)
            stmt = select(agg).where(*clauses)
            return float((await self.session.execute(stmt)).scalar_one())
        # LAST: the most recent record's quantity (by occurred_at).
        stmt = (
            select(BillingUsageRecord.quantity)
            .where(*clauses)
            .order_by(BillingUsageRecord.occurred_at.desc())
            .limit(1)
        )
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return float(value) if value is not None else 0.0

    async def count(self, *, subscription_id: str | None = None) -> int:
        clauses = []
        if subscription_id is not None:
            clauses.append(BillingUsageRecord.subscription_id == subscription_id)
        stmt = select(func.count()).select_from(BillingUsageRecord).where(*clauses)
        return int((await self.session.execute(stmt)).scalar_one())


class InvoiceRepo(BaseRepository):
    """Invoice header + lines persistence; the monotonic invoice-number sequence."""

    async def get(self, invoice_id: str) -> BillingInvoice | None:
        return await self.session.get(BillingInvoice, invoice_id)

    async def create(
        self, invoice: BillingInvoice, lines: list[BillingInvoiceLine]
    ) -> BillingInvoice:
        self.session.add(invoice)
        await self.session.flush()  # populate invoice.id for the line FKs
        for line in lines:
            line.invoice_id = invoice.id
            self.session.add(line)
        await self.session.flush()
        return invoice

    async def lines_for(self, invoice_id: str) -> list[BillingInvoiceLine]:
        stmt = select(BillingInvoiceLine).where(BillingInvoiceLine.invoice_id == invoice_id)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_for_subscription(self, subscription_id: str) -> list[BillingInvoice]:
        stmt = (
            select(BillingInvoice)
            .where(BillingInvoice.subscription_id == subscription_id)
            .order_by(BillingInvoice.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def next_sequence(self) -> int:
        """The next invoice sequence number (max existing + 1).

        A simple count-based sequence keyed off the rows that already have a
        number. Serialized in practice by the surrounding transaction; collisions
        on the UNIQUE ``number`` are surfaced as IntegrityError to the caller.
        """
        stmt = (
            select(func.count())
            .select_from(BillingInvoice)
            .where(BillingInvoice.number.is_not(None))
        )
        return int((await self.session.execute(stmt)).scalar_one()) + 1

    async def open_invoices(self, *, limit: int = 100) -> list[BillingInvoice]:
        stmt = (
            select(BillingInvoice).where(BillingInvoice.status == InvoiceStatus.OPEN).limit(limit)
        )
        return list((await self.session.execute(stmt)).scalars().all())


class PaymentAttemptRepo(BaseRepository):
    """Append-only payment-attempt (dunning) history."""

    async def record(self, attempt: BillingPaymentAttempt) -> BillingPaymentAttempt:
        self.session.add(attempt)
        await self.session.flush()
        return attempt

    async def for_invoice(self, invoice_id: str) -> list[BillingPaymentAttempt]:
        stmt = (
            select(BillingPaymentAttempt)
            .where(BillingPaymentAttempt.invoice_id == invoice_id)
            .order_by(BillingPaymentAttempt.attempt_number)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def attempt_count(self, invoice_id: str) -> int:
        stmt = (
            select(func.count())
            .select_from(BillingPaymentAttempt)
            .where(BillingPaymentAttempt.invoice_id == invoice_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())


class CouponRepo(BaseRepository):
    """Coupon persistence + redemption accounting."""

    async def get_by_code(self, code: str) -> BillingCoupon | None:
        stmt = select(BillingCoupon).where(BillingCoupon.code == code)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def create(self, coupon: BillingCoupon) -> BillingCoupon:
        self.session.add(coupon)
        await self.session.flush()
        return coupon

    async def increment_redeemed(self, code: str) -> None:
        coupon = await self.get_by_code(code)
        if coupon is not None:
            coupon.redeemed_count += 1
            await self.session.flush()


class WebhookRepo(BaseRepository):
    """Received-webhook idempotency + replay guard."""

    async def already_seen(self, *, provider: str, event_id: str) -> bool:
        stmt = select(BillingWebhookEvent.id).where(
            BillingWebhookEvent.provider == provider,
            BillingWebhookEvent.event_id == event_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None

    async def record(self, event: BillingWebhookEvent) -> bool:
        """Persist a received webhook; return False if it is a replay (duplicate)."""
        try:
            async with self.session.begin_nested():
                self.session.add(event)
                await self.session.flush()
        except IntegrityError:
            return False
        return True

    async def mark_processed(self, *, provider: str, event_id: str, at: datetime) -> None:
        stmt = select(BillingWebhookEvent).where(
            BillingWebhookEvent.provider == provider,
            BillingWebhookEvent.event_id == event_id,
        )
        row = (await self.session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            row.processed = True
            row.processed_at = at
            await self.session.flush()


class AuditRepo(BaseRepository):
    """Append-only billing audit ledger."""

    async def record(self, entry: BillingAuditLog) -> BillingAuditLog:
        self.session.add(entry)
        await self.session.flush()
        return entry

    async def for_subscription(self, subscription_id: str) -> list[BillingAuditLog]:
        stmt = (
            select(BillingAuditLog)
            .where(BillingAuditLog.subscription_id == subscription_id)
            .order_by(BillingAuditLog.occurred_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def for_customer(self, customer_id: str) -> list[BillingAuditLog]:
        stmt = (
            select(BillingAuditLog)
            .where(BillingAuditLog.customer_id == customer_id)
            .order_by(BillingAuditLog.occurred_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())


__all__ = [
    "AuditRepo",
    "CouponRepo",
    "CustomerRepo",
    "InvoiceRepo",
    "PaymentAttemptRepo",
    "PlanRepo",
    "SubscriptionRepo",
    "UsageRepo",
    "WebhookRepo",
]
