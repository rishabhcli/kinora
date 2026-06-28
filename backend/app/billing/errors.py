"""Billing-domain exception hierarchy.

All billing errors descend from :class:`BillingError` and carry a stable
machine-readable ``code`` so the API layer can map them to HTTP responses
(mirroring :class:`app.api.errors.APIError`) without leaking internals. The
domain raises these; the routes translate them.
"""

from __future__ import annotations


class BillingError(Exception):
    """Base class for every billing-domain error.

    Attributes:
        code: a stable, lowercase machine identifier (e.g. ``plan_not_found``).
        message: a human-readable description.
        http_status: the suggested HTTP status for the API layer.
    """

    code: str = "billing_error"
    http_status: int = 400

    def __init__(self, message: str, *, code: str | None = None, http_status: int | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status


class NotFoundError(BillingError):
    """A referenced billing entity does not exist."""

    code = "billing_not_found"
    http_status = 404


class PlanNotFoundError(NotFoundError):
    code = "plan_not_found"


class PriceNotFoundError(NotFoundError):
    code = "price_not_found"


class SubscriptionNotFoundError(NotFoundError):
    code = "subscription_not_found"


class InvoiceNotFoundError(NotFoundError):
    code = "invoice_not_found"


class CouponNotFoundError(NotFoundError):
    code = "coupon_not_found"


class CustomerNotFoundError(NotFoundError):
    code = "customer_not_found"


class InvalidStateError(BillingError):
    """An operation is illegal for the entity's current state."""

    code = "invalid_state"
    http_status = 409


class DuplicateError(BillingError):
    """A uniqueness invariant would be violated (e.g. plan code already exists)."""

    code = "duplicate"
    http_status = 409


class CouponInvalidError(BillingError):
    """A coupon is expired, redeemed out, or otherwise not applicable."""

    code = "coupon_invalid"
    http_status = 422


class EntitlementDeniedError(BillingError):
    """A feature/quota gate refused the action (the commercial guardrail)."""

    code = "entitlement_denied"
    http_status = 402  # Payment Required — the canonical "upgrade to continue".

    def __init__(
        self,
        message: str,
        *,
        feature: str | None = None,
        required_tier: str | None = None,
        meter: str | None = None,
        limit: float | None = None,
        used: float | None = None,
    ) -> None:
        super().__init__(message)
        self.feature = feature
        self.required_tier = required_tier
        self.meter = meter
        self.limit = limit
        self.used = used


class WebhookVerificationError(BillingError):
    """An inbound webhook failed signature/timestamp verification."""

    code = "webhook_invalid_signature"
    http_status = 400


class ProviderError(BillingError):
    """The payment provider transport returned/raised a failure."""

    code = "provider_error"
    http_status = 502


class CurrencyMismatchError(BillingError):
    """Money of two different currencies was combined."""

    code = "currency_mismatch"
    http_status = 400


__all__ = [
    "BillingError",
    "CouponInvalidError",
    "CouponNotFoundError",
    "CurrencyMismatchError",
    "CustomerNotFoundError",
    "DuplicateError",
    "EntitlementDeniedError",
    "InvalidStateError",
    "InvoiceNotFoundError",
    "NotFoundError",
    "PlanNotFoundError",
    "PriceNotFoundError",
    "ProviderError",
    "SubscriptionNotFoundError",
    "WebhookVerificationError",
]
