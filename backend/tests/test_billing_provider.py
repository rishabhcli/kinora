"""Tests for the payment-provider abstraction + fake transport + webhook signing.

These tests assert the hard rule: NO real Stripe/network call. The fake is fully
in-memory; the Stripe shape is unwired (raises NotImplementedError).
"""

from __future__ import annotations

import json
import time

import pytest

from app.billing.enums import PaymentStatus
from app.billing.errors import ProviderError, WebhookVerificationError
from app.billing.money import Money
from app.billing.provider import (
    FakePaymentProvider,
    PaymentProvider,
    ProviderConfig,
    StripePaymentProvider,
)
from app.billing.provider.signing import (
    build_signature_header,
    sign_payload,
    verify_signature,
)

# --- Protocol conformance --------------------------------------------------- #


def test_fake_is_a_payment_provider() -> None:
    provider = FakePaymentProvider()
    assert isinstance(provider, PaymentProvider)


def test_stripe_shape_is_a_payment_provider() -> None:
    provider = StripePaymentProvider()
    assert isinstance(provider, PaymentProvider)


# --- Fake provider lifecycle ------------------------------------------------ #


def test_create_customer_and_intent_and_settle() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email="reader@example.com")
    assert cust.id.startswith("cus_")
    intent = p.create_payment_intent(customer_id=cust.id, amount=Money(2900), invoice_id="in_1")
    assert intent.status is PaymentStatus.PENDING
    settled = p.confirm_payment_intent(intent.id)
    assert settled.succeeded
    charge = p.find_charge_for_intent(intent.id)
    assert charge is not None and charge.amount.amount_minor == 2900


def test_intent_unknown_customer_raises() -> None:
    p = FakePaymentProvider()
    with pytest.raises(ProviderError):
        p.create_payment_intent(customer_id="cus_nope", amount=Money(100))


def test_negative_amount_rejected() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email=None)
    with pytest.raises(ProviderError):
        p.create_payment_intent(customer_id=cust.id, amount=Money(-100))


def test_idempotent_intent_creation() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email=None)
    a = p.create_payment_intent(customer_id=cust.id, amount=Money(900), idempotency_key="k1")
    b = p.create_payment_intent(customer_id=cust.id, amount=Money(900), idempotency_key="k1")
    assert a.id == b.id


def test_fail_next_payment() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email=None)
    p.fail_next_payment(code="insufficient_funds", message="No funds")
    intent = p.create_payment_intent(customer_id=cust.id, amount=Money(900))
    failed = p.confirm_payment_intent(intent.id)
    assert failed.status is PaymentStatus.FAILED
    assert failed.failure_code == "insufficient_funds"
    # Failure clears — a retry can succeed.
    retry = p.confirm_payment_intent(intent.id)
    assert retry.succeeded


def test_sticky_failure() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email=None)
    p.set_failure(always=True)
    intent = p.create_payment_intent(customer_id=cust.id, amount=Money(900))
    assert p.confirm_payment_intent(intent.id).status is PaymentStatus.FAILED
    assert p.confirm_payment_intent(intent.id).status is PaymentStatus.FAILED
    p.set_failure(always=False)
    assert p.confirm_payment_intent(intent.id).succeeded


def test_confirm_settled_is_noop() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email=None)
    intent = p.create_payment_intent(customer_id=cust.id, amount=Money(900))
    first = p.confirm_payment_intent(intent.id)
    second = p.confirm_payment_intent(intent.id)
    assert first.id == second.id and second.succeeded


def test_refund_full_and_partial() -> None:
    p = FakePaymentProvider()
    cust = p.create_customer(email=None)
    intent = p.create_payment_intent(customer_id=cust.id, amount=Money(2000))
    p.confirm_payment_intent(intent.id)
    charge = p.find_charge_for_intent(intent.id)
    assert charge is not None
    refund = p.refund(charge_id=charge.id, amount=Money(500))
    assert refund.amount.amount_minor == 500
    assert p.charges[charge.id].refunded_amount is not None
    # Over-refund rejected.
    with pytest.raises(ProviderError):
        p.refund(charge_id=charge.id, amount=Money(99999))


def test_refund_unknown_charge() -> None:
    p = FakePaymentProvider()
    with pytest.raises(ProviderError):
        p.refund(charge_id="ch_nope")


# --- Webhook signing -------------------------------------------------------- #


def test_sign_and_verify_roundtrip() -> None:
    secret = "whsec_test"
    payload = b'{"id":"evt_1","type":"invoice.payment_succeeded"}'
    header = build_signature_header(payload, secret, timestamp=1_000_000)
    ts = verify_signature(payload, header, secret, now=1_000_000)
    assert ts == 1_000_000


def test_verify_rejects_tampered_payload() -> None:
    secret = "whsec_test"
    header = build_signature_header(b"original", secret, timestamp=1_000_000)
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"tampered", header, secret, now=1_000_000)


def test_verify_rejects_wrong_secret() -> None:
    header = build_signature_header(b"x", "secret_a", timestamp=1_000_000)
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"x", header, "secret_b", now=1_000_000)


def test_verify_rejects_stale_timestamp() -> None:
    secret = "whsec_test"
    header = build_signature_header(b"x", secret, timestamp=1_000_000)
    # 10 minutes later, default tolerance 300s -> stale.
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"x", header, secret, now=1_000_600, tolerance_s=300)


def test_verify_rejects_malformed_header() -> None:
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"x", "garbage", "secret", now=1)
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"x", "v1=abc", "secret", now=1)  # no timestamp
    with pytest.raises(WebhookVerificationError):
        verify_signature(b"x", "t=1", "secret", now=1)  # no v1


def test_sign_payload_uses_now_when_no_timestamp() -> None:
    ts, sig = sign_payload(b"x", "s")
    assert abs(ts - int(time.time())) < 5
    assert len(sig) == 64  # sha256 hex


# --- Fake webhook emit + verify loop ---------------------------------------- #


def test_fake_emit_and_parse_webhook() -> None:
    p = FakePaymentProvider()
    payload, header = p.emit_webhook(
        "invoice.payment_succeeded", {"invoice_id": "in_1"}, event_id="evt_42"
    )
    event = p.verify_and_parse_webhook(payload=payload, signature_header=header)
    assert event.id == "evt_42"
    assert event.type == "invoice.payment_succeeded"
    assert event.data == {"invoice_id": "in_1"}


def test_fake_webhook_bad_signature_rejected() -> None:
    p = FakePaymentProvider()
    payload, _ = p.emit_webhook("x", {})
    with pytest.raises(WebhookVerificationError):
        p.verify_and_parse_webhook(payload=payload, signature_header="t=1,v1=bad")


def test_custom_config_secret() -> None:
    cfg = ProviderConfig(name="fake", webhook_secret="whsec_custom")
    p = FakePaymentProvider(_config=cfg)
    payload, header = p.emit_webhook("x", {})
    # Verifying with the configured secret works.
    event = p.verify_and_parse_webhook(payload=payload, signature_header=header)
    assert event.type == "x"


# --- Stripe shape is unwired ------------------------------------------------ #


def test_stripe_methods_unimplemented() -> None:
    s = StripePaymentProvider()
    with pytest.raises(NotImplementedError):
        s.create_customer(email="x@example.com")
    with pytest.raises(NotImplementedError):
        s.create_payment_intent(customer_id="c", amount=Money(1))
    with pytest.raises(NotImplementedError):
        s.confirm_payment_intent("pi")
    with pytest.raises(NotImplementedError):
        s.refund(charge_id="ch")


def test_stripe_webhook_verify_is_offline_crypto() -> None:
    # The one offline-safe op: verifying an inbound signature, no network.
    cfg = ProviderConfig(name="stripe", webhook_secret="whsec_stripe")
    s = StripePaymentProvider(cfg)
    now = int(time.time())
    body = json.dumps(
        {"id": "evt_1", "type": "charge.refunded", "created": now, "data": {}}
    ).encode()
    header = build_signature_header(body, "whsec_stripe", timestamp=now)
    event = s.verify_and_parse_webhook(payload=body, signature_header=header)
    assert event.type == "charge.refunded"
    with pytest.raises(WebhookVerificationError):
        s.verify_and_parse_webhook(payload=body, signature_header="t=1,v1=nope")
