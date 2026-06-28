"""Time-proration and upgrade/downgrade credit math (pure functions).

When a reader changes plan mid-cycle, the unused remainder of the old plan is
**credited** and the new plan is **charged** for the time left in the period.
Stripe models this as two proration line items; we compute exactly those amounts
here, by *seconds* of the billing period for precision (a 30-day month and a
31-day month prorate differently).

All functions are pure and operate on :class:`app.billing.money.Money` +
timezone-aware UTC datetimes. The rounding convention matches the rest of the
domain (HALF_UP via :func:`app.billing.money.apply_rate`).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.billing.money import Money, apply_rate


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware (UTC)")


def period_fraction_remaining(
    *, period_start: datetime, period_end: datetime, at: datetime
) -> Decimal:
    """Fraction of the billing period still remaining at instant ``at`` (0..1).

    ``at`` before the start yields 1 (whole period remains); after the end
    yields 0. Computed by seconds so months of unequal length prorate correctly.
    """
    _require_aware("period_start", period_start)
    _require_aware("period_end", period_end)
    _require_aware("at", at)
    if period_end <= period_start:
        raise ValueError("period_end must be after period_start")
    total = Decimal((period_end - period_start).total_seconds())
    if at <= period_start:
        return Decimal(1)
    if at >= period_end:
        return Decimal(0)
    remaining = Decimal((period_end - at).total_seconds())
    return remaining / total


def period_fraction_elapsed(
    *, period_start: datetime, period_end: datetime, at: datetime
) -> Decimal:
    """Fraction of the billing period already elapsed at ``at`` (1 − remaining)."""
    return Decimal(1) - period_fraction_remaining(
        period_start=period_start, period_end=period_end, at=at
    )


def prorate_amount(
    amount: Money,
    *,
    period_start: datetime,
    period_end: datetime,
    at: datetime,
    remaining: bool = True,
) -> Money:
    """Prorate ``amount`` to the remaining (default) or elapsed slice of the period."""
    fraction = (
        period_fraction_remaining(period_start=period_start, period_end=period_end, at=at)
        if remaining
        else period_fraction_elapsed(period_start=period_start, period_end=period_end, at=at)
    )
    return apply_rate(amount, fraction)


@dataclass(frozen=True, slots=True)
class ProrationResult:
    """The two line amounts produced by a mid-period plan change.

    * ``credit`` — the unused remainder of the *old* plan, as a **negative**
      amount (a credit reduces the bill).
    * ``charge`` — the new plan prorated over the remaining period (positive).
    * ``net`` — ``charge + credit`` (what the immediate proration invoice totals).
    """

    credit: Money
    charge: Money

    @property
    def net(self) -> Money:
        return self.charge + self.credit


def compute_plan_change_proration(
    *,
    old_amount: Money,
    new_amount: Money,
    period_start: datetime,
    period_end: datetime,
    at: datetime,
) -> ProrationResult:
    """Credit the old plan's unused time and charge the new plan's remaining time.

    Both prices are assumed to cover the same billing period
    ``[period_start, period_end)``. The reader has consumed the elapsed slice on
    the old plan and will consume the remaining slice on the new one, so:

        credit = -prorate(old, remaining)
        charge =  prorate(new, remaining)
    """
    if old_amount.currency != new_amount.currency:
        raise ValueError("old/new prices must share a currency")
    remaining_old = prorate_amount(
        old_amount, period_start=period_start, period_end=period_end, at=at, remaining=True
    )
    remaining_new = prorate_amount(
        new_amount, period_start=period_start, period_end=period_end, at=at, remaining=True
    )
    return ProrationResult(credit=-remaining_old, charge=remaining_new)


def is_upgrade(old_amount: Money, new_amount: Money) -> bool:
    """True when the new recurring amount is strictly greater (an upgrade)."""
    return new_amount > old_amount


__all__ = [
    "ProrationResult",
    "compute_plan_change_proration",
    "is_upgrade",
    "period_fraction_elapsed",
    "period_fraction_remaining",
    "prorate_amount",
]
