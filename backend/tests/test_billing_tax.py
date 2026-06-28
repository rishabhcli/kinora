"""Tests for tax computation (inclusive/exclusive, multi-rate, resolver)."""

from __future__ import annotations

from decimal import Decimal

from app.billing.enums import TaxBehavior
from app.billing.money import Money
from app.billing.tax import TaxRate, TaxRateResolver, compute_tax


def test_no_rates_is_passthrough() -> None:
    result = compute_tax(Money(10000), [])
    assert result.total_tax.is_zero
    assert result.total_with_tax.amount_minor == 10000
    assert result.lines == ()


def test_exclusive_single_rate() -> None:
    result = compute_tax(Money(10000), [TaxRate("VAT", Decimal("0.20"))])
    assert result.total_tax.amount_minor == 2000
    assert result.total_with_tax.amount_minor == 12000
    assert result.base.amount_minor == 10000


def test_exclusive_multi_rate_stacks_on_base() -> None:
    # Country 5% + state 2.5% both on the $100 base -> 500 + 250 = 750.
    rates = [TaxRate("country", Decimal("0.05")), TaxRate("state", Decimal("0.025"))]
    result = compute_tax(Money(10000), rates)
    assert [line.amount.amount_minor for line in result.lines] == [500, 250]
    assert result.total_tax.amount_minor == 750
    assert result.total_with_tax.amount_minor == 10750
    assert result.combined_rate == Decimal("0.075")


def test_inclusive_single_rate_backs_out_tax() -> None:
    # $120 inclusive of 20% VAT -> base $100, tax $20.
    result = compute_tax(
        Money(12000), [TaxRate("VAT", Decimal("0.20"))], behavior=TaxBehavior.INCLUSIVE
    )
    assert result.base.amount_minor == 10000
    assert result.total_tax.amount_minor == 2000
    assert result.total_with_tax.amount_minor == 12000  # unchanged


def test_inclusive_multi_rate_lines_sum_to_total() -> None:
    rates = [TaxRate("a", Decimal("0.10")), TaxRate("b", Decimal("0.05"))]
    result = compute_tax(Money(11500), rates, behavior=TaxBehavior.INCLUSIVE)
    # base = 11500 / 1.15 = 10000; tax = 1500 split 10:5 -> 1000/500.
    assert result.base.amount_minor == 10000
    assert result.total_tax.amount_minor == 1500
    assert sum(line.amount.amount_minor for line in result.lines) == 1500
    assert [line.amount.amount_minor for line in result.lines] == [1000, 500]


def test_inclusive_zero_tax_when_no_rate() -> None:
    result = compute_tax(Money(10000), [], behavior=TaxBehavior.INCLUSIVE)
    assert result.total_tax.is_zero


def test_resolver_exact_and_country_fallback() -> None:
    r = TaxRateResolver.with_defaults()
    ca = r.resolve("US", "CA")
    assert len(ca) == 1 and ca[0].rate == Decimal("0.0725")
    gb = r.resolve("GB", None)
    assert gb[0].rate == Decimal("0.20")
    # Country with no region match and no country-level default -> [].
    assert r.resolve("US", "TX") == []
    # No country -> [].
    assert r.resolve(None, "CA") == []


def test_resolver_register_and_resolve() -> None:
    r = TaxRateResolver()
    r.register("FR", None, [TaxRate("FR VAT", Decimal("0.20"))])
    assert r.resolve("fr", None)[0].name == "FR VAT"


def test_negative_rate_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        TaxRate("bad", Decimal("-0.1"))


def test_tax_rounding_half_up() -> None:
    # 8.25% of $1.00 = 8.25¢ -> 8¢ (HALF_UP on .25 stays 8).
    result = compute_tax(Money(100), [TaxRate("x", Decimal("0.0825"))])
    assert result.total_tax.amount_minor == 8
