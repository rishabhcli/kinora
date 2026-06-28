"""The payment-provider protocol + its transport-neutral DTOs.

These DTOs are deliberately Stripe-shaped (``Customer``, ``PaymentIntent``,
``Charge``, ``Refund``, ``WebhookEvent``) so a real Stripe transport could
implement the same :class:`PaymentProvider` protocol without changing any caller.
But nothing here is Stripe-specific and nothing imports the Stripe SDK — the
default transport is the in-memory fake.

All amounts cross the boundary as ``(amount_minor, currency)`` integer pairs, the
same representation :class:`app.billing.money.Money` uses, so there is no
float/precision drift between the domain and the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.billing.enums import PaymentStatus
from app.billing.money import Money


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """Configuration the transport needs (a webhook secret, a provider name)."""

    name: str = "fake"
    webhook_secret: str = "whsec_fake_test_secret"
    #: Reject webhooks whose signed timestamp is older than this (replay window).
    webhook_tolerance_s: int = 300


@dataclass(frozen=True, slots=True)
class Customer:
    """A provider-side customer record."""

    id: str
    email: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PaymentIntent:
    """An attempt to collect a specific amount from a customer."""

    id: str
    customer_id: str
    amount: Money
    status: PaymentStatus
    invoice_id: str | None = None
    client_secret: str | None = None
    failure_code: str | None = None
    failure_message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status is PaymentStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class Charge:
    """A settled charge (the result of a succeeded payment intent)."""

    id: str
    payment_intent_id: str
    customer_id: str
    amount: Money
    refunded_amount: Money | None = None


@dataclass(frozen=True, slots=True)
class Refund:
    """A refund issued against a charge."""

    id: str
    charge_id: str
    amount: Money


@dataclass(frozen=True, slots=True)
class WebhookEvent:
    """A normalized inbound provider event (after signature verification)."""

    id: str
    type: str
    created: int  # unix seconds
    data: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class PaymentProvider(Protocol):
    """The provider surface the billing service depends on.

    Implementations: :class:`app.billing.provider.fake.FakePaymentProvider`
    (default, in-memory) and the shaped-but-unwired
    :class:`app.billing.provider.stripe.StripePaymentProvider`.
    """

    @property
    def config(self) -> ProviderConfig: ...

    def create_customer(
        self, *, email: str | None, metadata: dict[str, str] | None = None
    ) -> Customer:
        """Create (or return) a provider customer."""
        ...

    def create_payment_intent(
        self,
        *,
        customer_id: str,
        amount: Money,
        invoice_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentIntent:
        """Open a payment intent for ``amount`` against ``customer_id``."""
        ...

    def confirm_payment_intent(self, intent_id: str) -> PaymentIntent:
        """Attempt to settle a payment intent; returns the resulting status."""
        ...

    def refund(self, *, charge_id: str, amount: Money | None = None) -> Refund:
        """Refund a charge (full if ``amount`` is None)."""
        ...

    def verify_and_parse_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        """Verify the signature on a raw webhook body and parse it.

        Raises :class:`app.billing.errors.WebhookVerificationError` on a bad or
        stale signature.
        """
        ...


__all__ = [
    "Charge",
    "Customer",
    "PaymentIntent",
    "PaymentProvider",
    "ProviderConfig",
    "Refund",
    "WebhookEvent",
]
