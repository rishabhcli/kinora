"""Budget burndown + month-end spend forecast vs the cap (kinora.md §11.1).

A money-side complement to :mod:`app.finops.forecast` (which projects the
budget-critical *video-seconds*). This projects **USD spend** over the calendar
month from the historical daily-cost series and answers the questions an operator
dashboard asks:

* what's the **run-rate** ($/day) right now, from a trailing window?
* what's the **projected month-end spend** if the run-rate holds, and how does it
  compare to a ``$30``-style monthly cap?
* an **ETA-to-cap**: at the current run-rate, on what date does cumulative
  month-to-date spend cross the cap (``None`` if it never does this month)?
* a **burndown curve**: remaining budget (cap − cumulative spend) sampled per day
  to end-of-month, the line a dashboard draws.

Pure math over a list of ``(date, cost)`` points + a cap + an "as-of" date. No
I/O, no clock — the caller passes ``as_of``. Never raises on ordinary input;
degenerate inputs (no history, zero cap) yield well-defined neutral results.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class DailyCost:
    """One day's USD spend (the unit the burndown reads)."""

    day: date
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class BurndownPoint:
    """A sampled point on the remaining-budget curve."""

    day: date
    cumulative_usd: Decimal
    remaining_usd: Decimal
    projected: bool  # True once past ``as_of`` (forecast, not actuals)

    def as_dict(self) -> dict[str, Any]:
        return {
            "day": self.day.isoformat(),
            "cumulative_usd": str(self.cumulative_usd),
            "remaining_usd": str(self.remaining_usd),
            "projected": self.projected,
        }


@dataclass(frozen=True, slots=True)
class BurndownReport:
    """The full month-to-date burndown + forecast against a monthly cap."""

    as_of: date
    month_start: date
    month_end: date
    cap_usd: Decimal
    spent_mtd_usd: Decimal
    run_rate_usd_per_day: Decimal
    projected_month_end_usd: Decimal
    projected_overage_usd: Decimal
    will_exceed: bool
    eta_to_cap: date | None
    days_elapsed: int
    days_remaining: int
    curve: list[BurndownPoint] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "month_start": self.month_start.isoformat(),
            "month_end": self.month_end.isoformat(),
            "cap_usd": str(self.cap_usd),
            "spent_mtd_usd": str(self.spent_mtd_usd),
            "run_rate_usd_per_day": str(self.run_rate_usd_per_day),
            "projected_month_end_usd": str(self.projected_month_end_usd),
            "projected_overage_usd": str(self.projected_overage_usd),
            "will_exceed": self.will_exceed,
            "eta_to_cap": self.eta_to_cap.isoformat() if self.eta_to_cap else None,
            "days_elapsed": self.days_elapsed,
            "days_remaining": self.days_remaining,
            "curve": [p.as_dict() for p in self.curve],
        }


def _month_bounds(d: date) -> tuple[date, date]:
    """The first and last calendar day of ``d``'s month."""
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, 1), date(d.year, d.month, last)


def run_rate(
    daily: list[DailyCost], as_of: date, *, window_days: int = 7
) -> Decimal:
    """Trailing $/day run-rate over the last ``window_days`` up to ``as_of``.

    Averages over the *window length* (not just days that had spend), so a quiet
    weekend correctly pulls the run-rate down. Empty history → ``0``.
    """
    if window_days <= 0:
        return _ZERO
    start = as_of - timedelta(days=window_days - 1)
    total = sum(
        (dc.cost_usd for dc in daily if start <= dc.day <= as_of),
        _ZERO,
    )
    return (total / Decimal(window_days)).quantize(Decimal("0.000001"))


def build_burndown(
    daily: list[DailyCost],
    *,
    cap_usd: Decimal,
    as_of: date,
    run_rate_window_days: int = 7,
) -> BurndownReport:
    """Project month-end spend vs ``cap_usd`` from the daily-cost history.

    ``spent_mtd`` sums actuals from the 1st to ``as_of`` (inclusive). The forward
    forecast extends the trailing run-rate over the remaining days. The curve
    samples remaining budget per day for the whole month (actual then projected).
    """
    cap = cap_usd if isinstance(cap_usd, Decimal) else Decimal(str(cap_usd))
    month_start, month_end = _month_bounds(as_of)

    by_day: dict[date, Decimal] = {}
    for dc in daily:
        if month_start <= dc.day <= month_end:
            by_day[dc.day] = by_day.get(dc.day, _ZERO) + dc.cost_usd

    spent_mtd = sum(
        (cost for day, cost in by_day.items() if day <= as_of),
        _ZERO,
    )
    rate = run_rate(daily, as_of, window_days=run_rate_window_days)

    days_elapsed = (as_of - month_start).days + 1
    days_remaining = (month_end - as_of).days  # days strictly after as_of

    projected_end = spent_mtd + rate * Decimal(days_remaining)
    overage = projected_end - cap
    will_exceed = projected_end > cap

    eta = _eta_to_cap(spent_mtd, rate, cap, as_of, month_end)

    curve = _build_curve(by_day, cap, rate, month_start, month_end, as_of)

    return BurndownReport(
        as_of=as_of,
        month_start=month_start,
        month_end=month_end,
        cap_usd=cap,
        spent_mtd_usd=spent_mtd.quantize(Decimal("0.000001")),
        run_rate_usd_per_day=rate,
        projected_month_end_usd=projected_end.quantize(Decimal("0.000001")),
        projected_overage_usd=overage.quantize(Decimal("0.000001")),
        will_exceed=will_exceed,
        eta_to_cap=eta,
        days_elapsed=days_elapsed,
        days_remaining=days_remaining,
        curve=curve,
    )


def _eta_to_cap(
    spent_mtd: Decimal, rate: Decimal, cap: Decimal, as_of: date, month_end: date
) -> date | None:
    """The date cumulative spend crosses ``cap`` at ``rate`` (``None`` if never this month)."""
    if spent_mtd >= cap:
        return as_of  # already over — "ETA" is now
    if rate <= _ZERO:
        return None  # flat run-rate never reaches the cap
    remaining_budget = cap - spent_mtd
    days_until = remaining_budget / rate
    # ceil to the next whole day at/after which the cap is crossed.
    whole_days = int(days_until) + (1 if days_until % 1 != 0 else 0)
    eta = as_of + timedelta(days=whole_days)
    return eta if eta <= month_end else None


def _build_curve(
    by_day: dict[date, Decimal],
    cap: Decimal,
    rate: Decimal,
    month_start: date,
    month_end: date,
    as_of: date,
) -> list[BurndownPoint]:
    curve: list[BurndownPoint] = []
    cumulative = _ZERO
    day = month_start
    while day <= month_end:
        if day <= as_of:
            cumulative = cumulative + by_day.get(day, _ZERO)
            projected = False
        else:
            cumulative = cumulative + rate
            projected = True
        curve.append(
            BurndownPoint(
                day=day,
                cumulative_usd=cumulative.quantize(Decimal("0.000001")),
                remaining_usd=(cap - cumulative).quantize(Decimal("0.000001")),
                projected=projected,
            )
        )
        day = day + timedelta(days=1)
    return curve


def daily_from_isoseries(series: list[dict[str, Any]]) -> list[DailyCost]:
    """Adapt :func:`app.usageanalytics.aggregate.series` cost output to daily costs.

    Each point is ``{"bucket": iso, "value": decimal_str}``; the bucket's date is
    used. Non-day buckets are folded onto their date (multiple per day summed).
    """
    by_day: dict[date, Decimal] = {}
    for pt in series:
        raw = pt.get("bucket")
        val = pt.get("value")
        if raw is None or val is None:
            continue
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        day = dt.astimezone(UTC).date()
        by_day[day] = by_day.get(day, _ZERO) + Decimal(str(val))
    return [DailyCost(day=d, cost_usd=c) for d, c in sorted(by_day.items())]


__all__ = [
    "BurndownPoint",
    "BurndownReport",
    "DailyCost",
    "build_burndown",
    "daily_from_isoseries",
    "run_rate",
]
