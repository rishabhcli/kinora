"""ORM models for the billing tables (kinora.md §11 — commercial mirror).

These follow the project's model conventions exactly: string ids with the
``StrIdMixin`` default, timestamps via the shared mixins, enums stored as portable
``VARCHAR`` + named ``CHECK`` through :func:`app.db.models.enums.str_enum`, and a
fixed naming convention so Alembic autogenerate stays stable. Every table is
prefixed ``billing_`` so it cannot collide with another agent's additive tables.

Money is stored as two columns — ``*_minor`` (BigInteger minor units) +
``currency`` — never a float, matching :class:`app.billing.money.Money`. Ledgers
(usage records, payment attempts, audit log, webhook events) are append-only,
exactly like the budget ledger.

The enums are imported from :mod:`app.billing.enums`; ``str_enum`` builds each
column type with a unique constraint name so two billing tables that store the
same enum don't clash.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.billing.enums import (
    BillingInterval,
    CouponDuration,
    DiscountType,
    InvoiceStatus,
    MeteredAggregation,
    PaymentStatus,
    PlanTier,
    PriceType,
    SubscriptionStatus,
    TierMode,
    UsageMeter,
)
from app.db.base import Base, CreatedAtMixin, StrIdMixin, TimestampMixin


def str_enum(enum_cls: type[enum.Enum], name: str) -> SAEnum:
    """VARCHAR+CHECK column for ``enum_cls`` storing member values.

    A local copy of :func:`app.db.models.enums.str_enum` (identical behaviour),
    kept here so importing billing models does not pull in the ``app.db.models``
    package ``__init__`` (which itself imports this module — a circular import).
    Stored values are the enum *values* (the lowercase wire strings), portable
    across backends with ``native_enum=False``.
    """
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


class BillingPlan(StrIdMixin, TimestampMixin, Base):
    """A sellable plan tier in the catalog."""

    __tablename__ = "billing_plans"
    __table_args__ = (UniqueConstraint("code", name="uq_billing_plans_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    tier: Mapped[PlanTier] = mapped_column(str_enum(PlanTier, "billing_plan_tier"), nullable=False)
    trial_days: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Feature list as JSON: [{"key","label","limit"}].
    features: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    plan_metadata: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class BillingPrice(StrIdMixin, TimestampMixin, Base):
    """A price under a plan (flat / per-unit / metered, optionally tiered)."""

    __tablename__ = "billing_prices"
    __table_args__ = (Index("ix_billing_prices_plan", "plan_id"),)

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[PriceType] = mapped_column(
        str_enum(PriceType, "billing_price_type"), nullable=False
    )
    interval: Mapped[BillingInterval] = mapped_column(
        str_enum(BillingInterval, "billing_price_interval"), nullable=False
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    flat_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    unit_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Tiers as JSON: [{"up_to","unit_amount_minor","flat_amount_minor"}].
    tiers: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    tier_mode: Mapped[TierMode] = mapped_column(
        str_enum(TierMode, "billing_price_tier_mode"),
        default=TierMode.GRADUATED,
        nullable=False,
    )
    meter: Mapped[UsageMeter | None] = mapped_column(
        str_enum(UsageMeter, "billing_price_meter"), nullable=True
    )
    aggregation: Mapped[MeteredAggregation] = mapped_column(
        str_enum(MeteredAggregation, "billing_price_aggregation"),
        default=MeteredAggregation.SUM,
        nullable=False,
    )
    included_units: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(128), nullable=True)


class BillingCustomer(StrIdMixin, TimestampMixin, Base):
    """A user's billing identity + provider-customer mapping."""

    __tablename__ = "billing_customers"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_billing_customers_user_id"),
        Index("ix_billing_customers_provider", "provider_customer_id"),
    )

    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), default="fake", nullable=False)
    provider_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    default_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    tax_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    tax_region: Mapped[str | None] = mapped_column(String(8), nullable=True)
    delinquent: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class BillingSubscription(StrIdMixin, TimestampMixin, Base):
    """A customer's subscription to a plan (the lifecycle row)."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        Index("ix_billing_subscriptions_customer", "customer_id"),
        Index("ix_billing_subscriptions_status", "status"),
    )

    customer_id: Mapped[str] = mapped_column(
        ForeignKey("billing_customers.id", ondelete="CASCADE"), nullable=False
    )
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("billing_plans.id", ondelete="RESTRICT"), nullable=False
    )
    price_id: Mapped[str] = mapped_column(
        ForeignKey("billing_prices.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        str_enum(SubscriptionStatus, "billing_subscription_status"),
        default=SubscriptionStatus.INCOMPLETE,
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    current_period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    trial_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    coupon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Which billing period (0-based) the subscription is on — coupon duration math.
    period_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BillingSubscriptionItem(StrIdMixin, CreatedAtMixin, Base):
    """A per-price line item on a subscription (quantity for per-unit prices)."""

    __tablename__ = "billing_subscription_items"
    __table_args__ = (Index("ix_billing_subscription_items_sub", "subscription_id"),)

    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("billing_subscriptions.id", ondelete="CASCADE"), nullable=False
    )
    price_id: Mapped[str] = mapped_column(
        ForeignKey("billing_prices.id", ondelete="RESTRICT"), nullable=False
    )
    quantity: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class BillingUsageRecord(StrIdMixin, CreatedAtMixin, Base):
    """An append-only metered usage event (idempotent on ``idempotency_key``)."""

    __tablename__ = "billing_usage_records"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_billing_usage_records_idempotency_key"),
        Index("ix_billing_usage_records_scope", "subscription_id", "meter", "occurred_at"),
    )

    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("billing_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("billing_customers.id", ondelete="SET NULL"), nullable=True
    )
    meter: Mapped[UsageMeter] = mapped_column(
        str_enum(UsageMeter, "billing_usage_meter"), nullable=False
    )
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    book_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Nullable but UNIQUE: Postgres treats NULLs as distinct, so un-keyed events
    # (no idempotency) coexist while keyed ones dedup.
    idempotency_key: Mapped[str | None] = mapped_column(String(160), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class BillingCoupon(StrIdMixin, TimestampMixin, Base):
    """A reusable discount definition."""

    __tablename__ = "billing_coupons"
    __table_args__ = (UniqueConstraint("code", name="uq_billing_coupons_code"),)

    code: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    discount_type: Mapped[DiscountType] = mapped_column(
        str_enum(DiscountType, "billing_coupon_discount_type"), nullable=False
    )
    percent_off: Mapped[float | None] = mapped_column(Float, nullable=True)
    amount_off_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    duration: Mapped[CouponDuration] = mapped_column(
        str_enum(CouponDuration, "billing_coupon_duration"),
        default=CouponDuration.ONCE,
        nullable=False,
    )
    duration_in_periods: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_redemptions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    redeem_by: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    min_subtotal_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class BillingInvoice(StrIdMixin, TimestampMixin, Base):
    """An invoice header with reconciled totals."""

    __tablename__ = "billing_invoices"
    __table_args__ = (
        UniqueConstraint("number", name="uq_billing_invoices_number"),
        Index("ix_billing_invoices_subscription", "subscription_id"),
        Index("ix_billing_invoices_status", "status"),
    )

    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("billing_subscriptions.id", ondelete="SET NULL"), nullable=True
    )
    customer_id: Mapped[str | None] = mapped_column(
        ForeignKey("billing_customers.id", ondelete="SET NULL"), nullable=True
    )
    number: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[InvoiceStatus] = mapped_column(
        str_enum(InvoiceStatus, "billing_invoice_status"),
        default=InvoiceStatus.DRAFT,
        nullable=False,
    )
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    subtotal_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    discount_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    tax_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    total_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    amount_paid_minor: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    coupon_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class BillingInvoiceLine(StrIdMixin, CreatedAtMixin, Base):
    """A single charge/credit line on an invoice."""

    __tablename__ = "billing_invoice_lines"
    __table_args__ = (Index("ix_billing_invoice_lines_invoice", "invoice_id"),)

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    quantity: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    unit_amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    proration: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    price_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meter: Mapped[str | None] = mapped_column(String(64), nullable=True)


class BillingPaymentAttempt(StrIdMixin, CreatedAtMixin, Base):
    """An append-only record of one payment attempt (dunning history)."""

    __tablename__ = "billing_payment_attempts"
    __table_args__ = (Index("ix_billing_payment_attempts_invoice", "invoice_id"),)

    invoice_id: Mapped[str] = mapped_column(
        ForeignKey("billing_invoices.id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        str_enum(PaymentStatus, "billing_payment_status"), nullable=False
    )
    amount_minor: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)
    provider_intent_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingWebhookEvent(StrIdMixin, CreatedAtMixin, Base):
    """A received provider webhook (idempotency + replay guard)."""

    __tablename__ = "billing_webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "event_id", name="uq_billing_webhook_events_provider_event"),
    )

    provider: Mapped[str] = mapped_column(String(32), default="fake", nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BillingAuditLog(StrIdMixin, CreatedAtMixin, Base):
    """An append-only audit row for every billing mutation."""

    __tablename__ = "billing_audit_log"
    __table_args__ = (
        Index("ix_billing_audit_log_subscription", "subscription_id"),
        Index("ix_billing_audit_log_customer", "customer_id"),
        Index("ix_billing_audit_log_event", "event"),
    )

    event: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


__all__ = [
    "BillingAuditLog",
    "BillingCoupon",
    "BillingCustomer",
    "BillingInvoice",
    "BillingInvoiceLine",
    "BillingPaymentAttempt",
    "BillingPlan",
    "BillingPrice",
    "BillingSubscription",
    "BillingSubscriptionItem",
    "BillingUsageRecord",
    "BillingWebhookEvent",
]
