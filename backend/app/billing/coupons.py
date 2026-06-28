"""Coupon / discount math (pure).

A :class:`Coupon` is a reusable discount definition — a percentage or a fixed
amount off — with a *duration* describing how many invoices it keeps applying to
(once / forever / repeating for N months). It may also carry a redemption cap, a
validity window, and a minimum-spend floor.

The discount is always computed against a *pre-tax* subtotal and is clamped so it
never produces a negative line (you cannot owe a negative amount from a discount
alone). Fixed-amount coupons only apply in their own currency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.billing.enums import CouponDuration, DiscountType
from app.billing.errors import CouponInvalidError
from app.billing.money import Money, apply_rate


@dataclass(frozen=True, slots=True)
class Coupon:
    """A reusable discount definition."""

    code: str
    discount_type: DiscountType
    #: For PERCENT: a 0..100 percentage. For FIXED: ignored (use ``amount_off``).
    percent_off: Decimal | None = None
    #: For FIXED: the money amount taken off (in its own currency).
    amount_off: Money | None = None
    duration: CouponDuration = CouponDuration.ONCE
    #: For REPEATING: the number of billing periods it applies.
    duration_in_periods: int | None = None
    #: Optional redemption cap across all customers (None => unlimited).
    max_redemptions: int | None = None
    redeemed_count: int = 0
    #: Optional validity window (UTC, timezone-aware).
    redeem_by: datetime | None = None
    #: Optional minimum pre-tax subtotal (in minor units) for the coupon to apply.
    min_subtotal_minor: int | None = None
    active: bool = True

    def __post_init__(self) -> None:
        if self.discount_type is DiscountType.PERCENT:
            if self.percent_off is None:
                raise ValueError("percent coupon requires percent_off")
            if self.percent_off < 0 or self.percent_off > 100:
                raise ValueError("percent_off must be in [0, 100]")
        if self.discount_type is DiscountType.FIXED and self.amount_off is None:
            raise ValueError("fixed coupon requires amount_off")
        if self.duration is CouponDuration.REPEATING and not self.duration_in_periods:
            raise ValueError("repeating coupon requires duration_in_periods >= 1")

    def assert_redeemable(self, *, at: datetime, subtotal: Money) -> None:
        """Raise :class:`CouponInvalidError` if the coupon cannot apply now."""
        if not self.active:
            raise CouponInvalidError(f"coupon {self.code!r} is inactive")
        if self.redeem_by is not None:
            if at.tzinfo is None or self.redeem_by.tzinfo is None:
                raise ValueError("coupon validity comparison needs tz-aware datetimes")
            if at > self.redeem_by:
                raise CouponInvalidError(f"coupon {self.code!r} has expired")
        if self.max_redemptions is not None and self.redeemed_count >= self.max_redemptions:
            raise CouponInvalidError(f"coupon {self.code!r} is fully redeemed")
        if self.min_subtotal_minor is not None and subtotal.amount_minor < self.min_subtotal_minor:
            raise CouponInvalidError(
                f"coupon {self.code!r} needs a subtotal of at least "
                f"{self.min_subtotal_minor} minor units"
            )
        if self.discount_type is DiscountType.FIXED:
            assert self.amount_off is not None
            if self.amount_off.currency != subtotal.currency:
                raise CouponInvalidError(
                    f"coupon {self.code!r} is in {self.amount_off.currency}, "
                    f"subtotal is {subtotal.currency}"
                )

    def discount_for(self, subtotal: Money) -> Money:
        """The (non-negative) amount this coupon takes off ``subtotal``.

        Clamped to ``[0, subtotal]`` so a fixed coupon larger than the subtotal
        zeroes it rather than going negative.
        """
        if subtotal.amount_minor <= 0:
            return Money.zero(subtotal.currency)
        if self.discount_type is DiscountType.PERCENT:
            assert self.percent_off is not None
            raw = apply_rate(subtotal, self.percent_off / Decimal(100))
        else:
            assert self.amount_off is not None
            if self.amount_off.currency != subtotal.currency:
                raise CouponInvalidError("coupon currency mismatch")
            raw = self.amount_off
        # Clamp into [0, subtotal].
        if raw.amount_minor < 0:
            return Money.zero(subtotal.currency)
        if raw > subtotal:
            return subtotal
        return raw

    def applies_to_period_index(self, index: int) -> bool:
        """Whether the coupon still applies on the ``index``-th invoice (0-based).

        ONCE applies only to the first (index 0). FOREVER always applies.
        REPEATING applies for the first ``duration_in_periods`` invoices.
        """
        if index < 0:
            raise ValueError("period index must be >= 0")
        if self.duration is CouponDuration.ONCE:
            return index == 0
        if self.duration is CouponDuration.FOREVER:
            return True
        assert self.duration_in_periods is not None
        return index < self.duration_in_periods


def apply_coupon(
    subtotal: Money,
    coupon: Coupon | None,
    *,
    at: datetime,
    period_index: int = 0,
) -> tuple[Money, Money]:
    """Apply ``coupon`` to ``subtotal`` for the ``period_index``-th invoice.

    Returns ``(discount, discounted_subtotal)``. A ``None`` coupon, or one that
    does not apply this period, yields a zero discount. Raises
    :class:`CouponInvalidError` if a present coupon is not redeemable.
    """
    if coupon is None or not coupon.applies_to_period_index(period_index):
        return Money.zero(subtotal.currency), subtotal
    coupon.assert_redeemable(at=at, subtotal=subtotal)
    discount = coupon.discount_for(subtotal)
    return discount, subtotal - discount


__all__ = ["Coupon", "apply_coupon"]
