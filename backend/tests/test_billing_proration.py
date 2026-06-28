"""Tests for time-proration + upgrade/downgrade credit math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing.money import Money
from app.billing.proration import (
    compute_plan_change_proration,
    is_upgrade,
    period_fraction_elapsed,
    period_fraction_remaining,
    prorate_amount,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 31, tzinfo=UTC)  # 30-day period
MID = datetime(2026, 1, 16, tzinfo=UTC)  # exactly half (15 of 30 days)


def test_fraction_remaining_at_midpoint() -> None:
    frac = period_fraction_remaining(period_start=START, period_end=END, at=MID)
    assert frac == Decimal("0.5")


def test_fraction_clamps() -> None:
    before = START - timedelta(days=5)
    after = END + timedelta(days=5)
    assert period_fraction_remaining(period_start=START, period_end=END, at=before) == Decimal(1)
    assert period_fraction_remaining(period_start=START, period_end=END, at=after) == Decimal(0)


def test_fraction_elapsed_complements_remaining() -> None:
    assert period_fraction_elapsed(period_start=START, period_end=END, at=MID) == Decimal("0.5")


def test_requires_tz_aware() -> None:
    naive = datetime(2026, 1, 16)  # noqa: DTZ001 - intentional naive for the guard
    with pytest.raises(ValueError):
        period_fraction_remaining(period_start=START, period_end=END, at=naive)


def test_period_end_must_follow_start() -> None:
    with pytest.raises(ValueError):
        period_fraction_remaining(period_start=END, period_end=START, at=MID)


def test_prorate_amount_remaining() -> None:
    # $30.00 over the period, half remaining -> $15.00 credit/charge basis.
    out = prorate_amount(Money(3000), period_start=START, period_end=END, at=MID)
    assert out.amount_minor == 1500


def test_prorate_amount_elapsed() -> None:
    out = prorate_amount(Money(3000), period_start=START, period_end=END, at=MID, remaining=False)
    assert out.amount_minor == 1500


def test_plan_change_upgrade_proration() -> None:
    # Old $9/mo -> new $29/mo at midpoint: credit -$4.50 (half of old),
    # charge +$14.50 (half of new). Net +$10.00.
    result = compute_plan_change_proration(
        old_amount=Money(900),
        new_amount=Money(2900),
        period_start=START,
        period_end=END,
        at=MID,
    )
    assert result.credit.amount_minor == -450
    assert result.charge.amount_minor == 1450
    assert result.net.amount_minor == 1000


def test_plan_change_downgrade_proration_net_credit() -> None:
    # Downgrade $29 -> $9 at midpoint: credit -$14.50, charge +$4.50, net -$10.00.
    result = compute_plan_change_proration(
        old_amount=Money(2900),
        new_amount=Money(900),
        period_start=START,
        period_end=END,
        at=MID,
    )
    assert result.net.amount_minor == -1000
    assert result.net.is_negative


def test_plan_change_currency_must_match() -> None:
    with pytest.raises(ValueError):
        compute_plan_change_proration(
            old_amount=Money(900, "USD"),
            new_amount=Money(900, "EUR"),
            period_start=START,
            period_end=END,
            at=MID,
        )


def test_is_upgrade() -> None:
    assert is_upgrade(Money(900), Money(2900))
    assert not is_upgrade(Money(2900), Money(900))
    assert not is_upgrade(Money(900), Money(900))
