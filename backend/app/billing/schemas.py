"""Request/response DTOs for the billing API (transport contracts).

These are the wire shapes for the billing routes, distinct from the internal
domain value objects. Inputs validate untrusted client data (``extra="forbid"``);
outputs are plain dicts the service already produces, wrapped where a stable
envelope helps. Money always crosses the wire as integer minor units + a currency
string, never a float.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class CreateSubscriptionRequest(BaseModel):
    """Start a subscription for the authenticated user."""

    model_config = ConfigDict(extra="forbid")

    plan_code: str = Field(min_length=1, max_length=64)
    price_id: str | None = Field(default=None, max_length=64)
    interval: str = Field(default="month")
    coupon_code: str | None = Field(default=None, max_length=64)


class ChangePlanRequest(BaseModel):
    """Upgrade/downgrade the user's subscription."""

    model_config = ConfigDict(extra="forbid")

    new_plan_code: str = Field(min_length=1, max_length=64)
    new_price_id: str | None = Field(default=None, max_length=64)
    interval: str = Field(default="month")


class CancelSubscriptionRequest(BaseModel):
    """Cancel a subscription, at period end (default) or immediately."""

    model_config = ConfigDict(extra="forbid")

    at_period_end: bool = True


class RecordUsageRequest(BaseModel):
    """Report a metered usage event for the user's subscription."""

    model_config = ConfigDict(extra="forbid")

    meter: str = Field(min_length=1, max_length=64)
    quantity: float = Field(ge=0)
    idempotency_key: str | None = Field(default=None, max_length=160)
    book_id: str | None = Field(default=None, max_length=64)
    session_id: str | None = Field(default=None, max_length=64)


class PlanView(BaseModel):
    """One plan in the pricing-page list."""

    id: str
    code: str
    name: str
    tier: str
    trial_days: int
    description: str | None = None
    prices: list[dict[str, Any]]


class PlansResponse(BaseModel):
    """The active plan catalog."""

    plans: list[PlanView]


class SubscriptionView(BaseModel):
    """A subscription's transport shape (mirrors BillingService._sub_view)."""

    id: str
    customer_id: str
    plan_id: str
    price_id: str
    status: str
    currency: str
    current_period_start: str | None = None
    current_period_end: str | None = None
    trial_end: str | None = None
    cancel_at_period_end: bool = False
    coupon_code: str | None = None
    proration_invoice: dict[str, Any] | None = None


class InvoiceView(BaseModel):
    """An invoice's transport shape."""

    id: str
    number: str | None = None
    status: str
    currency: str
    subtotal_minor: int
    discount_minor: int
    tax_minor: int
    total_minor: int
    amount_paid_minor: int
    next_attempt_at: str | None = None
    attempt_count: int


class EntitlementsView(BaseModel):
    """The user's feature gates + per-meter allowances for the current plan."""

    tier: str
    plan_code: str
    active: bool
    features: dict[str, float | None]
    allowances: dict[str, int]


class UsageView(BaseModel):
    """Aggregated current-period usage by meter."""

    period_start: str | None = None
    period_end: str | None = None
    by_meter: dict[str, float]


class WebhookResponse(BaseModel):
    """The result of an inbound webhook."""

    event_id: str
    event_type: str
    status: str


__all__ = [
    "CancelSubscriptionRequest",
    "ChangePlanRequest",
    "CreateSubscriptionRequest",
    "EntitlementsView",
    "InvoiceView",
    "PlanView",
    "PlansResponse",
    "RecordUsageRequest",
    "SubscriptionView",
    "UsageView",
    "WebhookResponse",
]
