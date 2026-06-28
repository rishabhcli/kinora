"""Tests for the integer-minor-unit money primitives (app.billing.money)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.billing.money import (
    DEFAULT_CURRENCY,
    Money,
    apply_rate,
    minor_digits,
    minor_per_major,
    normalize_currency,
    sum_money,
)


def test_normalize_currency_uppercases_and_validates() -> None:
    assert normalize_currency("usd") == "USD"
    assert normalize_currency(" eur ") == "EUR"
    with pytest.raises(ValueError):
        normalize_currency("XYZ")


def test_minor_digits_and_per_major() -> None:
    assert minor_digits("USD") == 2
    assert minor_per_major("USD") == 100
    assert minor_digits("JPY") == 0
    assert minor_per_major("JPY") == 1


def test_from_major_usd() -> None:
    assert Money.from_major("9.99").amount_minor == 999
    assert Money.from_major("10").amount_minor == 1000
    assert Money.from_major(Decimal("0.01")).amount_minor == 1


def test_from_major_zero_minor_currency() -> None:
    # JPY has no minor units: 500 yen is 500 minor units.
    assert Money.from_major("500", "JPY").amount_minor == 500


def test_from_major_rejects_excess_precision() -> None:
    with pytest.raises(ValueError):
        Money.from_major("9.999")  # 3 fractional digits for USD
    with pytest.raises(ValueError):
        Money.from_major("1.5", "JPY")  # JPY allows none


def test_from_major_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money.from_major(9.99)  # type: ignore[arg-type]


def test_major_view_and_format() -> None:
    assert Money(999).major == Decimal("9.99")
    assert Money(999).format() == "9.99"
    assert Money(1000).format() == "10.00"
    assert Money(500, "JPY").format() == "500"


def test_zero_and_predicates() -> None:
    z = Money.zero("EUR")
    assert z.is_zero and z.currency == "EUR"
    assert Money(5).is_positive
    assert Money(-5).is_negative


def test_add_sub_neg() -> None:
    assert (Money(999) + Money(1)).amount_minor == 1000
    assert (Money(1000) - Money(1)).amount_minor == 999
    assert (-Money(999)).amount_minor == -999


def test_mul_by_int_quantity() -> None:
    assert (Money(250) * 3).amount_minor == 750
    assert (3 * Money(250)).amount_minor == 750
    with pytest.raises(TypeError):
        _ = Money(250) * 1.5  # type: ignore[operator]


def test_currency_mismatch_raises() -> None:
    with pytest.raises(ValueError):
        _ = Money(100, "USD") + Money(100, "EUR")
    with pytest.raises(ValueError):
        _ = Money(100, "USD") < Money(100, "EUR")


def test_ordering_within_currency() -> None:
    assert Money(100) < Money(200)
    assert Money(200) > Money(100)
    assert Money(100) <= Money(100)
    assert Money(100) >= Money(100)


def test_allocate_splits_with_no_lost_units() -> None:
    parts = Money(100).allocate([1, 1, 1])
    assert [p.amount_minor for p in parts] == [34, 33, 33]
    assert sum(p.amount_minor for p in parts) == 100


def test_allocate_weighted() -> None:
    # $10.00 split 70/30 -> 700/300; remainder-free here.
    parts = Money(1000).allocate([70, 30])
    assert [p.amount_minor for p in parts] == [700, 300]


def test_allocate_remainder_goes_to_heaviest_first() -> None:
    # 100 across weights [5, 3, 2]: floors are 50,30,20 -> sums exactly, no rem.
    # 101 across [5,3,2]: floors 50,30,20=100, remainder 1 -> heaviest (weight 5).
    parts = Money(101).allocate([5, 3, 2])
    assert [p.amount_minor for p in parts] == [51, 30, 20]


def test_allocate_negative_total() -> None:
    parts = Money(-100).allocate([1, 1, 1])
    assert [p.amount_minor for p in parts] == [-34, -33, -33]
    assert sum(p.amount_minor for p in parts) == -100


def test_allocate_validation() -> None:
    with pytest.raises(ValueError):
        Money(100).allocate([])
    with pytest.raises(ValueError):
        Money(100).allocate([0, 0])
    with pytest.raises(ValueError):
        Money(100).allocate([-1, 2])


def test_apply_rate_half_up_default() -> None:
    # 8.5% tax on $100.00 -> 850 cents exactly.
    assert apply_rate(Money(10000), Decimal("0.085")).amount_minor == 850
    # 8.25% of $1.00 = 8.25 cents -> HALF_UP -> 8.
    assert apply_rate(Money(100), Decimal("0.0825")).amount_minor == 8
    # 0.5 rounds up under HALF_UP.
    assert apply_rate(Money(1), Decimal("0.5")).amount_minor == 1


def test_apply_rate_banker() -> None:
    # 2.5 -> HALF_EVEN -> 2 (round to even).
    assert apply_rate(Money(5), Decimal("0.5"), banker=True).amount_minor == 2
    # 3.5 -> HALF_EVEN -> 4.
    assert apply_rate(Money(7), Decimal("0.5"), banker=True).amount_minor == 4


def test_apply_rate_rejects_float() -> None:
    with pytest.raises(TypeError):
        apply_rate(Money(100), 0.085)  # type: ignore[arg-type]


def test_sum_money_empty_and_nonempty() -> None:
    assert sum_money([], "USD") == Money.zero("USD")
    total = sum_money([Money(100), Money(250), Money(50)])
    assert total.amount_minor == 400


def test_default_currency_constant() -> None:
    assert Money(1).currency == DEFAULT_CURRENCY == "USD"
