"""A *shaped but deliberately unwired* Stripe transport.

This class implements the same :class:`app.billing.provider.base.PaymentProvider`
protocol as the fake, documenting exactly where a real Stripe integration would
slot in — **without importing the Stripe SDK and without ever making a network
call.** Every method that would touch Stripe raises :class:`NotImplementedError`
with a note explaining what it would do.

Why keep it: it proves the abstraction is genuinely provider-agnostic (the
service depends only on the protocol, never on the fake), and it gives a future
maintainer a precise contract to fill in. The composition root **never**
instantiates this — the default transport is always the in-memory fake, so no
real Stripe/network/payment call can happen.

The one method that is safe to implement without a network — webhook signature
verification — is implemented here using the same stdlib HMAC helper as the fake,
because verifying an *inbound* signature is pure local crypto.
"""

from __future__ import annotations

from app.billing.errors import ProviderError
from app.billing.money import Money
from app.billing.provider.base import (
    Customer,
    PaymentIntent,
    ProviderConfig,
    Refund,
    WebhookEvent,
)
from app.billing.provider.signing import verify_signature

_UNWIRED = (
    "StripePaymentProvider is intentionally unwired: this codebase makes no real "
    "Stripe/network/payment calls. Use FakePaymentProvider. To go live, implement "
    "this method against the Stripe SDK in a separate, explicit change."
)


class StripePaymentProvider:
    """The protocol-conformant Stripe shape — unwired by design."""

    def __init__(self, config: ProviderConfig | None = None) -> None:
        self._config = config or ProviderConfig(name="stripe")

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def create_customer(
        self, *, email: str | None, metadata: dict[str, str] | None = None
    ) -> Customer:
        raise NotImplementedError(_UNWIRED)

    def create_payment_intent(
        self,
        *,
        customer_id: str,
        amount: Money,
        invoice_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentIntent:
        raise NotImplementedError(_UNWIRED)

    def confirm_payment_intent(self, intent_id: str) -> PaymentIntent:
        raise NotImplementedError(_UNWIRED)

    def refund(self, *, charge_id: str, amount: Money | None = None) -> Refund:
        raise NotImplementedError(_UNWIRED)

    def verify_and_parse_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        """Verify an inbound webhook signature (pure local crypto; no network).

        This is the one operation a real Stripe transport can do offline, so it is
        implemented. Parsing the verified body into a :class:`WebhookEvent` is left
        unimplemented because the event-shape mapping is a live-integration detail.
        """
        import json
        import time

        verify_signature(
            payload,
            signature_header,
            self._config.webhook_secret,
            tolerance_s=self._config.webhook_tolerance_s,
        )
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("malformed webhook payload") from exc
        return WebhookEvent(
            id=str(body.get("id", "")),
            type=str(body.get("type", "")),
            created=int(body.get("created", int(time.time()))),
            data=dict(body.get("data", {})),
        )


__all__ = ["StripePaymentProvider"]
