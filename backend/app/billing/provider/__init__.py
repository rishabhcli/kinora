"""Payment-provider abstraction (Stripe-shaped) behind a fake transport.

The whole point of this subpackage is that **no real Stripe or network call is
ever made** — not in production wiring, not in tests. The :class:`PaymentProvider`
protocol defines the surface the billing service depends on; the default
:class:`FakePaymentProvider` is an in-memory implementation that simulates
customers, payment intents, refunds, and (most importantly) **signed inbound
webhooks**, so the entire payment + webhook loop can be exercised end-to-end with
zero external dependencies.

A :class:`StripePaymentProvider` is *shaped* (same protocol, same DTOs) but its
methods deliberately raise ``NotImplementedError`` — it documents how a real
transport would slot in without ever importing or calling the Stripe SDK. Wiring
it would be a separate, explicit decision; this package never does it.
"""

from __future__ import annotations

from app.billing.provider.base import (
    Charge,
    Customer,
    PaymentIntent,
    PaymentProvider,
    ProviderConfig,
    Refund,
    WebhookEvent,
)
from app.billing.provider.fake import FakePaymentProvider
from app.billing.provider.signing import (
    build_signature_header,
    sign_payload,
    verify_signature,
)
from app.billing.provider.stripe import StripePaymentProvider

__all__ = [
    "Charge",
    "Customer",
    "FakePaymentProvider",
    "PaymentIntent",
    "PaymentProvider",
    "ProviderConfig",
    "Refund",
    "StripePaymentProvider",
    "WebhookEvent",
    "build_signature_header",
    "sign_payload",
    "verify_signature",
]
