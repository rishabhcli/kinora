"""The in-memory fake payment provider — the default transport.

This is the ONLY provider the billing service is wired to. It simulates the
Stripe-shaped surface entirely in process memory: customers, payment intents,
charges, refunds, and **self-signed webhook events** (signed with the same secret
:func:`verify_and_parse_webhook` checks, so the inbound-webhook loop is fully
exercisable without a network).

Determinism + test control:

* ids are monotonic (``cus_1``, ``pi_1``, …) so assertions are stable;
* :meth:`fail_next_payment` / :meth:`set_failure` let a test script a declined
  card so dunning can be exercised;
* :meth:`emit_webhook` builds a correctly-signed ``(payload, header)`` pair for a
  given event so the webhook handler can be driven end-to-end.

No real Stripe import, no socket, no spend.
"""

from __future__ import annotations

import itertools
import json
import time
from dataclasses import dataclass, field

from app.billing.enums import PaymentStatus
from app.billing.errors import ProviderError
from app.billing.money import Money
from app.billing.provider.base import (
    Charge,
    Customer,
    PaymentIntent,
    ProviderConfig,
    Refund,
    WebhookEvent,
)
from app.billing.provider.signing import build_signature_header, verify_signature


@dataclass
class FakePaymentProvider:
    """An in-memory :class:`app.billing.provider.base.PaymentProvider`."""

    _config: ProviderConfig = field(default_factory=ProviderConfig)
    customers: dict[str, Customer] = field(default_factory=dict)
    intents: dict[str, PaymentIntent] = field(default_factory=dict)
    charges: dict[str, Charge] = field(default_factory=dict)
    refunds: dict[str, Refund] = field(default_factory=dict)
    #: When set, the next confirm fails with this (code, message); then clears.
    _next_failure: tuple[str, str] | None = None
    #: When True, every confirm fails until cleared (sticky decline).
    _always_fail: bool = False
    _idempotency: dict[str, str] = field(default_factory=dict)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    # -- protocol ------------------------------------------------------------ #

    @property
    def config(self) -> ProviderConfig:
        return self._config

    def create_customer(
        self, *, email: str | None, metadata: dict[str, str] | None = None
    ) -> Customer:
        cid = f"cus_{next(self._seq)}"
        customer = Customer(id=cid, email=email, metadata=dict(metadata or {}))
        self.customers[cid] = customer
        return customer

    def create_payment_intent(
        self,
        *,
        customer_id: str,
        amount: Money,
        invoice_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> PaymentIntent:
        if customer_id not in self.customers:
            raise ProviderError(f"unknown customer {customer_id!r}")
        if amount.is_negative:
            raise ProviderError("cannot charge a negative amount")
        # Idempotent intent creation (a retried request returns the same intent).
        if idempotency_key is not None and idempotency_key in self._idempotency:
            return self.intents[self._idempotency[idempotency_key]]
        pid = f"pi_{next(self._seq)}"
        intent = PaymentIntent(
            id=pid,
            customer_id=customer_id,
            amount=amount,
            status=PaymentStatus.PENDING,
            invoice_id=invoice_id,
            client_secret=f"{pid}_secret",
        )
        self.intents[pid] = intent
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = pid
        return intent

    def confirm_payment_intent(self, intent_id: str) -> PaymentIntent:
        intent = self.intents.get(intent_id)
        if intent is None:
            raise ProviderError(f"unknown payment intent {intent_id!r}")
        if intent.status is PaymentStatus.SUCCEEDED:
            return intent  # confirming a settled intent is a no-op

        if self._always_fail or self._next_failure is not None:
            code, message = self._next_failure or ("card_declined", "Your card was declined.")
            self._next_failure = None
            failed = PaymentIntent(
                id=intent.id,
                customer_id=intent.customer_id,
                amount=intent.amount,
                status=PaymentStatus.FAILED,
                invoice_id=intent.invoice_id,
                client_secret=intent.client_secret,
                failure_code=code,
                failure_message=message,
            )
            self.intents[intent_id] = failed
            return failed

        # Zero-amount intents settle trivially (e.g. a fully-credited invoice).
        succeeded = PaymentIntent(
            id=intent.id,
            customer_id=intent.customer_id,
            amount=intent.amount,
            status=PaymentStatus.SUCCEEDED,
            invoice_id=intent.invoice_id,
            client_secret=intent.client_secret,
        )
        self.intents[intent_id] = succeeded
        charge_id = f"ch_{next(self._seq)}"
        self.charges[charge_id] = Charge(
            id=charge_id,
            payment_intent_id=intent.id,
            customer_id=intent.customer_id,
            amount=intent.amount,
        )
        return succeeded

    def refund(self, *, charge_id: str, amount: Money | None = None) -> Refund:
        charge = self.charges.get(charge_id)
        if charge is None:
            raise ProviderError(f"unknown charge {charge_id!r}")
        refund_amount = amount if amount is not None else charge.amount
        if refund_amount > charge.amount:
            raise ProviderError("refund exceeds the charged amount")
        rid = f"re_{next(self._seq)}"
        refund = Refund(id=rid, charge_id=charge_id, amount=refund_amount)
        self.refunds[rid] = refund
        self.charges[charge_id] = Charge(
            id=charge.id,
            payment_intent_id=charge.payment_intent_id,
            customer_id=charge.customer_id,
            amount=charge.amount,
            refunded_amount=refund_amount,
        )
        return refund

    def verify_and_parse_webhook(self, *, payload: bytes, signature_header: str) -> WebhookEvent:
        verify_signature(
            payload,
            signature_header,
            self._config.webhook_secret,
            tolerance_s=self._config.webhook_tolerance_s,
        )
        body = json.loads(payload.decode("utf-8"))
        return WebhookEvent(
            id=str(body.get("id", "")),
            type=str(body.get("type", "")),
            created=int(body.get("created", int(time.time()))),
            data=dict(body.get("data", {})),
        )

    # -- test / simulation helpers ------------------------------------------ #

    def fail_next_payment(self, *, code: str = "card_declined", message: str = "Declined") -> None:
        """Make the next :meth:`confirm_payment_intent` fail (then clear)."""
        self._next_failure = (code, message)

    def set_failure(self, *, always: bool) -> None:
        """Toggle a sticky decline (every confirm fails until cleared)."""
        self._always_fail = always

    def find_charge_for_intent(self, intent_id: str) -> Charge | None:
        """The charge settled for a payment intent (if any)."""
        for charge in self.charges.values():
            if charge.payment_intent_id == intent_id:
                return charge
        return None

    def emit_webhook(
        self,
        event_type: str,
        data: dict[str, object],
        *,
        event_id: str | None = None,
        timestamp: int | None = None,
    ) -> tuple[bytes, str]:
        """Build a correctly-signed ``(payload, signature_header)`` for an event.

        The handler can feed this straight into :meth:`verify_and_parse_webhook`,
        so the full inbound loop runs with no external provider.
        """
        eid = event_id or f"evt_{next(self._seq)}"
        body = {
            "id": eid,
            "type": event_type,
            "created": timestamp if timestamp is not None else int(time.time()),
            "data": data,
        }
        payload = json.dumps(body).encode("utf-8")
        header = build_signature_header(payload, self._config.webhook_secret, timestamp=timestamp)
        return payload, header


__all__ = ["FakePaymentProvider"]
