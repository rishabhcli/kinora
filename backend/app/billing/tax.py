"""Tax computation (pure): inclusive/exclusive, multi-rate, jurisdiction resolver.

Two tax behaviours are supported (see :class:`app.billing.enums.TaxBehavior`):

* **EXCLUSIVE** — listed prices are pre-tax; tax is *added on top*. The displayed
  total = subtotal + tax.
* **INCLUSIVE** — listed prices already contain the tax; we *back out* the tax
  component for reporting. The displayed total == subtotal; tax is the portion
  inside it.

A :class:`TaxRate` is a named rate for a jurisdiction; a :class:`TaxResult`
breaks a subtotal into its taxable base, the per-rate amounts, and the total. A
tiny in-memory :class:`TaxRateResolver` maps a (country, region) tuple to the
applicable rates — enough to demonstrate multi-rate (e.g. country + state) math
without a tax-service network call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.billing.enums import TaxBehavior
from app.billing.money import Money, apply_rate


@dataclass(frozen=True, slots=True)
class TaxRate:
    """A named tax rate for a jurisdiction (e.g. ``"CA state"`` at 7.25%)."""

    name: str
    rate: Decimal  # fractional, e.g. Decimal("0.0725")
    jurisdiction: str = ""

    def __post_init__(self) -> None:
        if self.rate < 0:
            raise ValueError("tax rate must be non-negative")


@dataclass(frozen=True, slots=True)
class TaxLine:
    """The computed tax amount for one applied :class:`TaxRate`."""

    name: str
    rate: Decimal
    amount: Money


@dataclass(frozen=True, slots=True)
class TaxResult:
    """The breakdown of tax over a subtotal."""

    behavior: TaxBehavior
    base: Money  # the pre-tax base the rates applied to
    lines: tuple[TaxLine, ...]
    total_tax: Money
    total_with_tax: Money

    @property
    def combined_rate(self) -> Decimal:
        """Sum of the applied fractional rates."""
        return sum((line.rate for line in self.lines), Decimal(0))


def compute_tax(
    subtotal: Money,
    rates: list[TaxRate],
    *,
    behavior: TaxBehavior = TaxBehavior.EXCLUSIVE,
) -> TaxResult:
    """Compute tax over ``subtotal`` for a set of (possibly stacked) ``rates``.

    Multiple rates **stack additively on the same base** (country + state both
    applied to the pre-tax subtotal) — the common destination-tax convention —
    rather than compounding. Each rate's amount is rounded independently so the
    per-line amounts always sum to the reported total.
    """
    currency = subtotal.currency
    if not rates:
        return TaxResult(
            behavior=behavior,
            base=subtotal if behavior is TaxBehavior.EXCLUSIVE else subtotal,
            lines=(),
            total_tax=Money.zero(currency),
            total_with_tax=subtotal,
        )

    combined = sum((r.rate for r in rates), Decimal(0))

    if behavior is TaxBehavior.EXCLUSIVE:
        base = subtotal
        lines = tuple(
            TaxLine(name=r.name, rate=r.rate, amount=apply_rate(base, r.rate)) for r in rates
        )
        total_tax = Money(sum(line.amount.amount_minor for line in lines), currency)
        return TaxResult(
            behavior=behavior,
            base=base,
            lines=lines,
            total_tax=total_tax,
            total_with_tax=subtotal + total_tax,
        )

    # INCLUSIVE: the subtotal already contains tax at the combined rate.
    #   base = subtotal / (1 + combined); tax = subtotal - base.
    divisor = Decimal(1) + combined
    base_minor = int((Decimal(subtotal.amount_minor) / divisor).quantize(Decimal(1)))
    base = Money(base_minor, currency)
    # Split the inclusive tax across rates proportionally to each rate.
    total_tax = subtotal - base
    lines = _split_inclusive_tax(total_tax, rates, combined)
    return TaxResult(
        behavior=behavior,
        base=base,
        lines=lines,
        total_tax=total_tax,
        total_with_tax=subtotal,
    )


def _split_inclusive_tax(
    total_tax: Money, rates: list[TaxRate], combined: Decimal
) -> tuple[TaxLine, ...]:
    """Allocate an inclusive ``total_tax`` across ``rates`` proportionally."""
    if combined == 0 or total_tax.is_zero:
        return tuple(TaxLine(r.name, r.rate, Money.zero(total_tax.currency)) for r in rates)
    # Use the remainder-safe allocator with weights = scaled rates.
    weights = [int((r.rate * Decimal(1_000_000)).to_integral_value()) for r in rates]
    if sum(weights) == 0:  # pragma: no cover - guarded by combined != 0
        return tuple(TaxLine(r.name, r.rate, Money.zero(total_tax.currency)) for r in rates)
    parts = total_tax.allocate(weights)
    return tuple(TaxLine(r.name, r.rate, amt) for r, amt in zip(rates, parts, strict=True))


@dataclass
class TaxRateResolver:
    """A tiny in-memory (country, region) -> rates lookup.

    Demonstrates multi-rate resolution (e.g. US state sales tax stacked on a
    federal-less base, or an EU country VAT) without any external tax service.
    Keys are ``(country_upper, region_upper_or_empty)``.
    """

    table: dict[tuple[str, str], list[TaxRate]] = field(default_factory=dict)

    def register(self, country: str, region: str | None, rates: list[TaxRate]) -> None:
        self.table[(country.upper(), (region or "").upper())] = list(rates)

    def resolve(self, country: str | None, region: str | None) -> list[TaxRate]:
        """Best-match rates: exact (country, region), then (country, ''), else []."""
        if not country:
            return []
        c = country.upper()
        r = (region or "").upper()
        if (c, r) in self.table:
            return list(self.table[(c, r)])
        if (c, "") in self.table:
            return list(self.table[(c, "")])
        return []

    @classmethod
    def with_defaults(cls) -> TaxRateResolver:
        """A resolver seeded with a few illustrative jurisdictions."""
        resolver = cls()
        resolver.register(
            "US",
            "CA",
            [TaxRate("CA sales tax", Decimal("0.0725"), "US-CA")],
        )
        resolver.register(
            "US",
            "NY",
            [TaxRate("NY sales tax", Decimal("0.04"), "US-NY")],
        )
        resolver.register("GB", None, [TaxRate("UK VAT", Decimal("0.20"), "GB")])
        resolver.register("DE", None, [TaxRate("DE VAT", Decimal("0.19"), "DE")])
        return resolver


__all__ = [
    "TaxLine",
    "TaxRate",
    "TaxRateResolver",
    "TaxResult",
    "compute_tax",
]
