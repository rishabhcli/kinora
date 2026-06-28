"""Tests for the append-only billing audit ledger."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.billing.audit import AuditEntry, AuditLog, replay_amount
from app.billing.enums import AuditEvent

T = datetime(2026, 6, 1, tzinfo=UTC)


def test_entry_requires_aware() -> None:
    with pytest.raises(ValueError):
        AuditEntry(event=AuditEvent.INVOICE_PAID, at=datetime(2026, 6, 1))  # noqa: DTZ001


def test_record_and_query() -> None:
    log = AuditLog()
    log.record(
        AuditEvent.SUBSCRIPTION_CREATED,
        at=T,
        actor="user_1",
        subscription_id="sub_1",
        customer_id="cus_1",
    )
    log.record(
        AuditEvent.INVOICE_PAID,
        at=T,
        subscription_id="sub_1",
        invoice_id="in_1",
        amount_minor=2900,
        currency="USD",
    )
    assert len(log) == 2
    assert len(log.for_subscription("sub_1")) == 2
    assert len(log.for_customer("cus_1")) == 1
    assert len(log.of_type(AuditEvent.INVOICE_PAID)) == 1


def test_record_returns_entry_with_detail() -> None:
    log = AuditLog()
    entry = log.record(AuditEvent.COUPON_APPLIED, at=T, subscription_id="sub_1", code="SAVE20")
    assert entry.event is AuditEvent.COUPON_APPLIED
    assert entry.detail == {"code": "SAVE20"}


def test_default_timestamp_is_now() -> None:
    log = AuditLog()
    entry = log.record(AuditEvent.USAGE_RECORDED)
    assert entry.at.tzinfo is not None


def test_replay_amount() -> None:
    log = AuditLog()
    log.record(AuditEvent.INVOICE_PAID, at=T, amount_minor=2900)
    log.record(AuditEvent.PAYMENT_FAILED, at=T, amount_minor=0)
    log.record(AuditEvent.INVOICE_PAID, at=T, amount_minor=900)
    assert replay_amount(log.entries()) == 3800


def test_entries_is_immutable_snapshot() -> None:
    log = AuditLog()
    log.record(AuditEvent.WEBHOOK_RECEIVED, at=T)
    snapshot = log.entries()
    log.record(AuditEvent.WEBHOOK_REPLAYED, at=T)
    assert len(snapshot) == 1  # snapshot didn't grow
    assert len(log) == 2
