"""Enumerations for the billing domain.

These mirror the project convention (``app/db/models/enums.py``): every enum is a
:class:`enum.StrEnum` whose *value* is the lowercase wire string, and DB columns
store them as portable ``VARCHAR`` + named ``CHECK`` via ``str_enum`` (no Postgres
ENUM type). They're defined here, in the domain, so the pure (DB-free) services
can import them without dragging in SQLAlchemy.
"""

from __future__ import annotations

import enum


class BillingInterval(enum.StrEnum):
    """The recurrence period of a recurring price."""

    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class PlanTier(enum.StrEnum):
    """Coarse plan tier — drives default entitlements and upgrade/downgrade math."""

    FREE = "free"
    STARTER = "starter"
    PRO = "pro"
    STUDIO = "studio"
    ENTERPRISE = "enterprise"


class PriceType(enum.StrEnum):
    """Whether a price is a flat recurring fee or billed by metered usage."""

    FLAT = "flat"  # fixed amount per period
    PER_UNIT = "per_unit"  # quantity * unit amount (licensed seats etc.)
    METERED = "metered"  # billed against recorded usage (reading-minutes / render-seconds)


class MeteredAggregation(enum.StrEnum):
    """How metered usage in a period collapses to a billable quantity."""

    SUM = "sum"  # total usage over the period (reading-minutes)
    MAX = "max"  # peak usage in the period
    LAST = "last"  # last reported value (e.g. seat count)


class TierMode(enum.StrEnum):
    """How a tiered price computes a charge across its bands."""

    #: Each tier's per-unit rate applies only to the units that fall in it.
    GRADUATED = "graduated"
    #: One tier (the one the *total* quantity lands in) prices the whole quantity.
    VOLUME = "volume"


class UsageMeter(enum.StrEnum):
    """The metrics Kinora meters for usage-based billing.

    These are the commercial mirror of the video-seconds budget ledger (§11):
    ``RENDER_SECONDS`` is the reader-facing analogue of provider video-seconds,
    and ``READING_MINUTES`` captures consumption even when no video is rendered.
    """

    READING_MINUTES = "reading_minutes"
    RENDER_SECONDS = "render_seconds"
    BOOKS_IMPORTED = "books_imported"
    DIRECTOR_EDITS = "director_edits"


class SubscriptionStatus(enum.StrEnum):
    """Subscription lifecycle (Stripe-shaped so the abstraction maps cleanly)."""

    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"  # a payment failed; dunning is retrying
    UNPAID = "unpaid"  # dunning exhausted; access should be gated
    CANCELED = "canceled"
    INCOMPLETE = "incomplete"  # awaiting first payment confirmation
    INCOMPLETE_EXPIRED = "incomplete_expired"
    PAUSED = "paused"


class InvoiceStatus(enum.StrEnum):
    """Invoice lifecycle."""

    DRAFT = "draft"
    OPEN = "open"  # finalized, awaiting payment
    PAID = "paid"
    UNCOLLECTIBLE = "uncollectible"  # written off after dunning
    VOID = "void"


class PaymentStatus(enum.StrEnum):
    """Outcome of a single payment attempt against an invoice."""

    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REQUIRES_ACTION = "requires_action"  # e.g. 3DS / SCA
    CANCELED = "canceled"


class CouponDuration(enum.StrEnum):
    """How long a coupon's discount keeps applying."""

    ONCE = "once"  # the next invoice only
    FOREVER = "forever"  # every invoice while attached
    REPEATING = "repeating"  # for N months


class DiscountType(enum.StrEnum):
    """Whether a coupon takes off a percentage or a fixed amount."""

    PERCENT = "percent"
    FIXED = "fixed"


class TaxBehavior(enum.StrEnum):
    """Whether listed prices already include tax or tax is added on top."""

    INCLUSIVE = "inclusive"
    EXCLUSIVE = "exclusive"


class ProrationBehavior(enum.StrEnum):
    """What happens to the unused/used portion on a mid-period plan change."""

    CREATE_PRORATIONS = "create_prorations"  # credit unused + charge new (default)
    NONE = "none"  # no proration; change applies next period
    ALWAYS_INVOICE = "always_invoice"  # prorate and bill immediately


class AuditEvent(enum.StrEnum):
    """The mutation kinds recorded in the append-only billing audit ledger."""

    CUSTOMER_CREATED = "customer_created"
    SUBSCRIPTION_CREATED = "subscription_created"
    SUBSCRIPTION_UPDATED = "subscription_updated"
    SUBSCRIPTION_CANCELED = "subscription_canceled"
    TRIAL_STARTED = "trial_started"
    TRIAL_ENDED = "trial_ended"
    USAGE_RECORDED = "usage_recorded"
    INVOICE_CREATED = "invoice_created"
    INVOICE_FINALIZED = "invoice_finalized"
    INVOICE_PAID = "invoice_paid"
    INVOICE_VOIDED = "invoice_voided"
    PAYMENT_ATTEMPTED = "payment_attempted"
    PAYMENT_SUCCEEDED = "payment_succeeded"
    PAYMENT_FAILED = "payment_failed"
    DUNNING_SCHEDULED = "dunning_scheduled"
    DUNNING_EXHAUSTED = "dunning_exhausted"
    COUPON_APPLIED = "coupon_applied"
    WEBHOOK_RECEIVED = "webhook_received"
    WEBHOOK_REPLAYED = "webhook_replayed"


class ProviderEventType(enum.StrEnum):
    """Inbound provider webhook event types we understand (Stripe-shaped names)."""

    PAYMENT_SUCCEEDED = "invoice.payment_succeeded"
    PAYMENT_FAILED = "invoice.payment_failed"
    SUBSCRIPTION_UPDATED = "customer.subscription.updated"
    SUBSCRIPTION_DELETED = "customer.subscription.deleted"
    CHARGE_REFUNDED = "charge.refunded"


__all__ = [
    "AuditEvent",
    "BillingInterval",
    "CouponDuration",
    "DiscountType",
    "InvoiceStatus",
    "MeteredAggregation",
    "PaymentStatus",
    "PlanTier",
    "PriceType",
    "ProrationBehavior",
    "ProviderEventType",
    "SubscriptionStatus",
    "TaxBehavior",
    "TierMode",
    "UsageMeter",
]
