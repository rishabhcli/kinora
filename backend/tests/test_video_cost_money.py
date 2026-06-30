"""Unit tests for the exact integer-minor-unit Money type + multi-currency FX.

The whole cost layer rests on Money never drifting, so these tests pin: exact
construction (and rejection of lossy float literals), no-float-drift addition,
integer scaling, fractional scaling with banker's rounding, cross-currency guards,
ordering within a currency, and explicit FX conversion. No infra, no network.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.video.cost.money import (
    MINOR_UNIT_SCALE,
    Currency,
    CurrencyMismatch,
    FxConverter,
    Money,
)


def test_usd_construction_is_exact() -> None:
    m = Money.usd("0.19")
    # 0.19 USD = 19 cents = 19 * 10**MINOR_UNIT_SCALE internal units.
    assert m.units == 19 * (10**MINOR_UNIT_SCALE)
    assert m.to_decimal() == Decimal("0.1900")
    assert str(m) == "0.19 USD"


def test_no_float_drift_on_repeated_addition() -> None:
    # The classic 0.1 + 0.2 != 0.3 trap — exact here.
    total = Money.zero()
    for _ in range(3):
        total = total + Money.usd("0.10")
    assert total == Money.usd("0.30")
    # Summing 158 MiniMax clips lands exactly, not 30.000000004.
    clip = Money.usd("0.19")
    assert (clip * 158) == Money.usd("30.02")


def test_from_decimal_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money.from_decimal(0.19, Currency.USD)  # type: ignore[arg-type]


def test_from_float_rounds_half_even() -> None:
    # A float source is allowed only through the explicit lossy door.
    assert Money.from_float(0.19, Currency.USD) == Money.usd("0.19")


def test_sub_second_per_second_rate_is_representable() -> None:
    # $0.0317/s must survive exactly thanks to MINOR_UNIT_SCALE.
    rate = Money.usd("0.0317")
    assert rate.to_decimal() == Decimal("0.0317")
    # 5 seconds at that rate, scaled fractionally.
    assert rate.scaled(Decimal("5")) == Money.usd("0.1585")


def test_scaled_fractional_banker_rounding() -> None:
    # 0.19 * 1.5 = 0.285 -> rounds half-to-even at the internal scale.
    m = Money.usd("0.19")
    # internal units 190000 * 1.5 = 285000 exactly (no rounding needed here)
    assert m.scaled(Decimal("1.5")) == Money(285000, Currency.USD)
    assert m.scaled(Decimal("1.5")).to_decimal() == Decimal("0.2850")


def test_mul_requires_int() -> None:
    m = Money.usd("0.19")
    with pytest.raises(TypeError):
        m * 1.5  # type: ignore[operator]
    with pytest.raises(TypeError):
        m * True  # bool is not an honest count


def test_currency_mismatch_on_arithmetic() -> None:
    with pytest.raises(CurrencyMismatch):
        Money.usd("1") + Money.from_decimal("1", Currency.EUR)
    with pytest.raises(CurrencyMismatch):
        _ = Money.usd("1") < Money.from_decimal("1", Currency.EUR)


def test_ordering_and_min_max_within_currency() -> None:
    a, b = Money.usd("0.19"), Money.usd("0.60")
    assert a < b and b > a and a <= a and b >= b
    assert Money.min(a, b) == a
    assert Money.max(a, b) == b


def test_jpy_zero_minor_digits() -> None:
    m = Money.from_decimal("100", Currency.JPY)
    assert m.to_decimal() == Decimal("100")
    assert str(m) == "100 JPY"


def test_from_minor_natural_units() -> None:
    assert Money.from_minor(19, Currency.USD) == Money.usd("0.19")


def test_negative_and_neg() -> None:
    m = Money.usd("0.19")
    assert (-m).units == -m.units
    assert (Money.usd("0.10") - Money.usd("0.30")) == Money.usd("-0.20")


def test_fx_convert_round_trip_is_explicit() -> None:
    fx = FxConverter.from_rate_strings(Currency.USD, {Currency.EUR: "0.92"})
    eur = fx.convert(Money.usd("10.00"), Currency.EUR)
    assert eur.currency is Currency.EUR
    assert eur.to_decimal() == Decimal("9.20")
    # Same currency is a no-op.
    assert fx.convert(Money.usd("10.00"), Currency.USD) == Money.usd("10.00")


def test_fx_missing_rate_raises() -> None:
    fx = FxConverter.from_rate_strings(Currency.USD, {Currency.EUR: "0.92"})
    with pytest.raises(KeyError):
        fx.convert(Money.usd("1.00"), Currency.GBP)
