"""Cost-ledger reconciliation + micro-USD conversion (kinora.md §11.1). Pure."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.db.repositories.finops import micros_to_usd, usd_to_micros
from app.finops.ledger import reconcile


def test_usd_micros_roundtrip() -> None:
    assert usd_to_micros(Decimal("1.234567")) == 1_234_567
    assert micros_to_usd(1_234_567) == Decimal("1.234567")
    # Rounding to the nearest micro.
    assert usd_to_micros(Decimal("0.0000004")) == 0
    assert usd_to_micros(Decimal("0.0000006")) == 1


def test_reconcile_within_tolerance() -> None:
    r = reconcile(
        scope_label="book=b1",
        budget_committed_s=100.0,
        cost_recorded_s=100.05,
        tolerance_s=0.1,
    )
    assert r.reconciled
    assert r.drift_s == pytest.approx(0.05)
    assert r.abs_drift_s == pytest.approx(0.05)


def test_reconcile_drift_beyond_tolerance() -> None:
    r = reconcile(
        scope_label="book=b1",
        budget_committed_s=100.0,
        cost_recorded_s=95.0,
        tolerance_s=0.1,
    )
    assert not r.reconciled
    assert r.drift_s == pytest.approx(-5.0)
    assert r.abs_drift_s == pytest.approx(5.0)


def test_reconcile_exact_match() -> None:
    r = reconcile(scope_label="global", budget_committed_s=50.0, cost_recorded_s=50.0)
    assert r.reconciled
    assert r.drift_s == 0.0


def test_reconcile_as_dict_serializable() -> None:
    r = reconcile(scope_label="s", budget_committed_s=10.0, cost_recorded_s=9.5)
    d = r.as_dict()
    assert set(d) >= {"scope", "budget_committed_s", "cost_recorded_s", "drift_s", "reconciled"}
    assert d["reconciled"] is False
