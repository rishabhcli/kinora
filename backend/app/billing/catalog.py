"""Plan + price catalog and the pure pricing-math that evaluates a quantity.

A **plan** is a sellable product tier (Free / Starter / Pro / Studio). A plan
exposes one or more **prices** — a flat recurring fee, a per-unit (seat) price,
or a *metered* price billed against recorded usage. Metered/per-unit prices can
be **tiered**: graduated (each band priced at its own rate) or volume (one band
prices the whole quantity).

Everything in this module is an immutable in-memory dataclass + **pure
functions**. The DB-backed catalog (``repositories.py`` over ``models.py``)
hydrates these objects; the math here never touches the database, so it is
trivially testable and deterministic.

The metered prices are the commercial face of the §11 budget: ``RENDER_SECONDS``
maps to the same scarce video-seconds the budget ledger reserves, and
``READING_MINUTES`` captures consumption that costs nothing in credits but is
still a billable unit of value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.billing.enums import (
    BillingInterval,
    MeteredAggregation,
    PlanTier,
    PriceType,
    TierMode,
    UsageMeter,
)
from app.billing.errors import PriceNotFoundError
from app.billing.money import DEFAULT_CURRENCY, Money, apply_rate


@dataclass(frozen=True, slots=True)
class PriceTier:
    """One band of a tiered price.

    ``up_to`` is the inclusive upper bound of the band in units; ``None`` means
    the final, unbounded band. A tier prices its units at ``unit_amount`` (per
    unit) and may add a per-period ``flat_amount`` when the quantity reaches it.
    """

    up_to: int | None
    unit_amount: Money
    flat_amount: Money | None = None


@dataclass(frozen=True, slots=True)
class Price:
    """A sellable price under a plan.

    For ``FLAT`` prices only ``flat_amount`` matters. For ``PER_UNIT`` the charge
    is ``unit_amount * quantity`` (or the tiers, if present). For ``METERED`` the
    quantity comes from aggregated usage and ``meter`` / ``aggregation`` say which
    metric and how to collapse it.
    """

    id: str
    plan_code: str
    type: PriceType
    interval: BillingInterval
    currency: str = DEFAULT_CURRENCY
    flat_amount: Money | None = None
    unit_amount: Money | None = None
    tiers: tuple[PriceTier, ...] = ()
    tier_mode: TierMode = TierMode.GRADUATED
    meter: UsageMeter | None = None
    aggregation: MeteredAggregation = MeteredAggregation.SUM
    #: Units bundled into the flat fee before metered/per-unit charges start.
    included_units: int = 0
    nickname: str | None = None

    def __post_init__(self) -> None:
        if self.type is PriceType.FLAT and self.flat_amount is None:
            raise ValueError("flat price requires flat_amount")
        if self.type is PriceType.PER_UNIT and self.unit_amount is None and not self.tiers:
            raise ValueError("per-unit price requires unit_amount or tiers")
        if self.type is PriceType.METERED:
            if self.meter is None:
                raise ValueError("metered price requires a meter")
            if self.unit_amount is None and not self.tiers:
                raise ValueError("metered price requires unit_amount or tiers")
        self._validate_tiers()

    def _validate_tiers(self) -> None:
        if not self.tiers:
            return
        bounds = [t.up_to for t in self.tiers]
        # All but the last must be bounded, ascending; the last is the catch-all.
        for b in bounds[:-1]:
            if b is None:
                raise ValueError("only the final tier may be unbounded (up_to=None)")
        finite = [b for b in bounds if b is not None]
        if finite != sorted(finite) or len(set(finite)) != len(finite):
            raise ValueError("tier up_to bounds must be strictly ascending")

    @property
    def is_recurring(self) -> bool:
        return True  # every catalog price is recurring; one-offs are invoice lines

    def charge_for(self, quantity: int) -> Money:
        """Compute the period charge for ``quantity`` units of this price."""
        return compute_price_charge(self, quantity)


@dataclass(frozen=True, slots=True)
class Feature:
    """A named capability a plan can grant.

    ``limit`` is an optional numeric quota for the feature (e.g. max books). A
    feature with ``limit=None`` is an unlimited/boolean capability; the gate just
    checks presence.
    """

    key: str
    label: str
    limit: float | None = None


@dataclass(frozen=True, slots=True)
class Plan:
    """A sellable plan tier with its prices, features, and trial policy."""

    code: str
    name: str
    tier: PlanTier
    prices: tuple[Price, ...] = ()
    features: tuple[Feature, ...] = ()
    trial_days: int = 0
    active: bool = True
    description: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)

    def price(self, price_id: str) -> Price:
        """Return the price with ``price_id`` or raise :class:`PriceNotFoundError`."""
        for p in self.prices:
            if p.id == price_id:
                return p
        raise PriceNotFoundError(f"no price {price_id!r} on plan {self.code!r}")

    def primary_price(self, interval: BillingInterval | None = None) -> Price | None:
        """A reasonable default price (first matching interval, else first)."""
        if interval is not None:
            for p in self.prices:
                if p.interval is interval:
                    return p
        return self.prices[0] if self.prices else None

    def feature(self, key: str) -> Feature | None:
        for f in self.features:
            if f.key == key:
                return f
        return None

    def has_feature(self, key: str) -> bool:
        return self.feature(key) is not None


# --------------------------------------------------------------------------- #
# Pure pricing math
# --------------------------------------------------------------------------- #


def compute_tiered_charge(
    tiers: tuple[PriceTier, ...], quantity: int, mode: TierMode, currency: str
) -> Money:
    """Price ``quantity`` units across ``tiers`` under graduated/volume mode.

    *Graduated*: each tier's rate applies only to the units that fall inside it
    (like a marginal tax bracket). *Volume*: the single tier the *total* quantity
    lands in prices the whole quantity.
    """
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    if not tiers:
        raise ValueError("compute_tiered_charge needs at least one tier")
    if quantity == 0:
        return Money.zero(currency)

    if mode is TierMode.VOLUME:
        tier = _tier_for_quantity(tiers, quantity)
        charge = tier.unit_amount * quantity
        if tier.flat_amount is not None:
            charge = charge + tier.flat_amount
        return charge

    # Graduated: walk the bands, charging the slice of the quantity in each.
    total = Money.zero(currency)
    lower = 0
    remaining = quantity
    for tier in tiers:
        upper = tier.up_to if tier.up_to is not None else quantity + lower
        band_capacity = upper - lower
        units_here = min(remaining, band_capacity)
        if units_here <= 0:
            break
        total = total + tier.unit_amount * units_here
        if tier.flat_amount is not None:
            total = total + tier.flat_amount
        remaining -= units_here
        lower = upper
        if remaining <= 0:
            break
    return total


def _tier_for_quantity(tiers: tuple[PriceTier, ...], quantity: int) -> PriceTier:
    """The tier a total ``quantity`` lands in (volume mode)."""
    for tier in tiers:
        if tier.up_to is None or quantity <= tier.up_to:
            return tier
    return tiers[-1]


def compute_price_charge(price: Price, quantity: int) -> Money:
    """The period charge for ``quantity`` units of ``price`` (after inclusions)."""
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    currency = price.currency

    if price.type is PriceType.FLAT:
        assert price.flat_amount is not None  # guarded in __post_init__
        return price.flat_amount

    billable = max(0, quantity - price.included_units)
    if billable == 0:
        return Money.zero(currency)

    if price.tiers:
        return compute_tiered_charge(price.tiers, billable, price.tier_mode, currency)

    assert price.unit_amount is not None  # guarded in __post_init__
    return price.unit_amount * billable


def aggregate_usage(values: list[float], aggregation: MeteredAggregation) -> float:
    """Collapse a period's raw usage ``values`` to a single billable quantity."""
    if not values:
        return 0.0
    if aggregation is MeteredAggregation.SUM:
        return float(sum(values))
    if aggregation is MeteredAggregation.MAX:
        return float(max(values))
    if aggregation is MeteredAggregation.LAST:
        return float(values[-1])
    raise ValueError(f"unknown aggregation {aggregation!r}")  # pragma: no cover


def annualized(amount: Money, interval: BillingInterval) -> Money:
    """Project a per-interval amount onto a yearly figure (for comparison UIs).

    Uses simple 12-month / 52-week / 365-day conventions; this is a display
    helper, never used for actual charging.
    """
    factors: dict[BillingInterval, int] = {
        BillingInterval.DAY: 365,
        BillingInterval.WEEK: 52,
        BillingInterval.MONTH: 12,
        BillingInterval.YEAR: 1,
    }
    return amount * factors[interval]


def discount_percent_off(amount: Money, percent: Decimal | str | int) -> Money:
    """The amount taken off ``amount`` for a ``percent`` discount (0-100)."""
    pct = Decimal(percent) if not isinstance(percent, Decimal) else percent
    if pct < 0 or pct > 100:
        raise ValueError("percent must be in [0, 100]")
    return apply_rate(amount, pct / Decimal(100))


__all__ = [
    "Feature",
    "Plan",
    "Price",
    "PriceTier",
    "aggregate_usage",
    "annualized",
    "compute_price_charge",
    "compute_tiered_charge",
    "discount_percent_off",
]
