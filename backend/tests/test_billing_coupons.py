"""Tests for coupon/discount math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.billing.coupons import Coupon, apply_coupon
from app.billing.enums import CouponDuration, DiscountType
from app.billing.errors import CouponInvalidError
from app.billing.money import Money

NOW = datetime(2026, 6, 28, tzinfo=UTC)


def test_percent_coupon_validation() -> None:
    with pytest.raises(ValueError):
        Coupon(code="X", discount_type=DiscountType.PERCENT)  # no percent
    with pytest.raises(ValueError):
        Coupon(code="X", discount_type=DiscountType.PERCENT, percent_off=Decimal("150"))


def test_fixed_coupon_requires_amount() -> None:
    with pytest.raises(ValueError):
        Coupon(code="X", discount_type=DiscountType.FIXED)


def test_repeating_requires_periods() -> None:
    with pytest.raises(ValueError):
        Coupon(
            code="X",
            discount_type=DiscountType.PERCENT,
            percent_off=Decimal("10"),
            duration=CouponDuration.REPEATING,
        )


def test_percent_discount() -> None:
    c = Coupon(code="SAVE20", discount_type=DiscountType.PERCENT, percent_off=Decimal("20"))
    assert c.discount_for(Money(10000)).amount_minor == 2000


def test_fixed_discount() -> None:
    c = Coupon(code="OFF5", discount_type=DiscountType.FIXED, amount_off=Money(500))
    assert c.discount_for(Money(10000)).amount_minor == 500


def test_fixed_discount_clamped_to_subtotal() -> None:
    c = Coupon(code="OFF50", discount_type=DiscountType.FIXED, amount_off=Money(5000))
    # Subtotal only $10 — discount clamps to $10, never negative.
    assert c.discount_for(Money(1000)).amount_minor == 1000


def test_discount_on_zero_subtotal() -> None:
    c = Coupon(code="X", discount_type=DiscountType.PERCENT, percent_off=Decimal("50"))
    assert c.discount_for(Money(0)).is_zero


def test_expired_coupon_rejected() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        redeem_by=NOW - timedelta(days=1),
    )
    with pytest.raises(CouponInvalidError):
        c.assert_redeemable(at=NOW, subtotal=Money(1000))


def test_inactive_coupon_rejected() -> None:
    c = Coupon(
        code="X", discount_type=DiscountType.PERCENT, percent_off=Decimal("10"), active=False
    )
    with pytest.raises(CouponInvalidError):
        c.assert_redeemable(at=NOW, subtotal=Money(1000))


def test_max_redemptions_rejected() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        max_redemptions=5,
        redeemed_count=5,
    )
    with pytest.raises(CouponInvalidError):
        c.assert_redeemable(at=NOW, subtotal=Money(1000))


def test_min_subtotal_rejected() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        min_subtotal_minor=5000,
    )
    with pytest.raises(CouponInvalidError):
        c.assert_redeemable(at=NOW, subtotal=Money(1000))
    c.assert_redeemable(at=NOW, subtotal=Money(5000))  # exactly at floor: ok


def test_fixed_currency_mismatch_rejected() -> None:
    c = Coupon(code="X", discount_type=DiscountType.FIXED, amount_off=Money(500, "EUR"))
    with pytest.raises(CouponInvalidError):
        c.assert_redeemable(at=NOW, subtotal=Money(1000, "USD"))


def test_duration_once_only_first_period() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        duration=CouponDuration.ONCE,
    )
    assert c.applies_to_period_index(0)
    assert not c.applies_to_period_index(1)


def test_duration_forever() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        duration=CouponDuration.FOREVER,
    )
    assert c.applies_to_period_index(0)
    assert c.applies_to_period_index(99)


def test_duration_repeating() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        duration=CouponDuration.REPEATING,
        duration_in_periods=3,
    )
    assert [c.applies_to_period_index(i) for i in range(5)] == [True, True, True, False, False]


def test_apply_coupon_none() -> None:
    discount, after = apply_coupon(Money(1000), None, at=NOW)
    assert discount.is_zero and after.amount_minor == 1000


def test_apply_coupon_not_this_period() -> None:
    c = Coupon(
        code="X",
        discount_type=DiscountType.PERCENT,
        percent_off=Decimal("10"),
        duration=CouponDuration.ONCE,
    )
    discount, after = apply_coupon(Money(1000), c, at=NOW, period_index=1)
    assert discount.is_zero and after.amount_minor == 1000


def test_apply_coupon_happy_path() -> None:
    c = Coupon(code="X", discount_type=DiscountType.PERCENT, percent_off=Decimal("25"))
    discount, after = apply_coupon(Money(2000), c, at=NOW)
    assert discount.amount_minor == 500
    assert after.amount_minor == 1500
