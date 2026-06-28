"""Money as integer minor units — the only safe way to do billing arithmetic.

Floating-point money is a recurring source of off-by-a-cent invoice bugs, so the
whole billing domain represents amounts as **integer minor units** (cents for
USD/EUR, pence for GBP, and — crucially — *whole yen* for JPY, which has zero
minor digits). A :class:`Money` is an ``(amount_minor, currency)`` pair; all
arithmetic stays in integers, and the only place rounding happens is the two
explicit, well-tested primitives :func:`apply_rate` (percent/ratio math, e.g.
tax and proration) and :meth:`Money.allocate` (remainder-safe splitting).

This mirrors the budget ledger's discipline (``app/db/models/budget.py``): a
single, auditable representation with no silent precision loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Final

#: Minor-unit exponents for the currencies Kinora prices in. ISO-4217 "minor
#: unit" digit counts: most are 2, JPY/KRW have 0. Extend as new currencies ship.
CURRENCY_MINOR_DIGITS: Final[dict[str, int]] = {
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "CAD": 2,
    "AUD": 2,
    "SGD": 2,
    "INR": 2,
    "CNY": 2,
    "JPY": 0,
    "KRW": 0,
}

#: The default presentation/charge currency for the platform.
DEFAULT_CURRENCY: Final[str] = "USD"


def normalize_currency(code: str) -> str:
    """Upper-case + validate a currency code against the supported set."""
    norm = code.strip().upper()
    if norm not in CURRENCY_MINOR_DIGITS:
        raise ValueError(f"unsupported currency: {code!r}")
    return norm


def minor_digits(currency: str) -> int:
    """Number of minor-unit digits for ``currency`` (2 for USD, 0 for JPY)."""
    return CURRENCY_MINOR_DIGITS[normalize_currency(currency)]


def minor_per_major(currency: str) -> int:
    """Minor units in one major unit (100 for USD, 1 for JPY)."""
    return 10 ** minor_digits(currency)


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An amount in integer minor units, tagged with its currency.

    Negative amounts are allowed (credits / refunds / proration credits). Two
    :class:`Money` values may only be combined when their currencies match —
    cross-currency arithmetic raises rather than guessing an exchange rate.
    """

    amount_minor: int
    currency: str = DEFAULT_CURRENCY

    def __post_init__(self) -> None:
        # Validate + canonicalize the currency without breaking frozen-ness.
        object.__setattr__(self, "currency", normalize_currency(self.currency))
        if not isinstance(self.amount_minor, int):  # pragma: no cover - typing guard
            raise TypeError("amount_minor must be an int (minor units)")

    # -- constructors -------------------------------------------------------- #

    @classmethod
    def zero(cls, currency: str = DEFAULT_CURRENCY) -> Money:
        """The additive identity in ``currency``."""
        return cls(0, currency)

    @classmethod
    def from_major(cls, major: str | int | Decimal, currency: str = DEFAULT_CURRENCY) -> Money:
        """Build from a major-unit value (``"9.99"`` USD -> 999 cents).

        Accepts strings/ints/Decimals (never floats) so callers cannot smuggle
        binary-float imprecision into a price. The major value must have no more
        fractional digits than the currency allows.
        """
        currency = normalize_currency(currency)
        if isinstance(major, float):  # pragma: no cover - typing guard
            raise TypeError("pass major amounts as str/int/Decimal, never float")
        dec = Decimal(major)
        scaled = dec * minor_per_major(currency)
        if scaled != scaled.to_integral_value():
            raise ValueError(f"{major} has more fractional digits than {currency} supports")
        return cls(int(scaled), currency)

    # -- views --------------------------------------------------------------- #

    @property
    def major(self) -> Decimal:
        """The amount as an exact major-unit :class:`Decimal` (for display)."""
        return Decimal(self.amount_minor) / minor_per_major(self.currency)

    @property
    def is_zero(self) -> bool:
        return self.amount_minor == 0

    @property
    def is_positive(self) -> bool:
        return self.amount_minor > 0

    @property
    def is_negative(self) -> bool:
        return self.amount_minor < 0

    def format(self) -> str:
        """A bare numeric string at the currency's precision (e.g. ``"9.99"``)."""
        digits = minor_digits(self.currency)
        if digits == 0:
            return str(self.amount_minor)
        quant = Decimal(1).scaleb(-digits)
        return str(self.major.quantize(quant))

    # -- arithmetic ---------------------------------------------------------- #

    def _check(self, other: Money) -> None:
        if self.currency != other.currency:
            raise ValueError(f"currency mismatch: {self.currency} vs {other.currency}")

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor + other.amount_minor, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.amount_minor - other.amount_minor, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount_minor, self.currency)

    def __mul__(self, qty: int) -> Money:
        if not isinstance(qty, int):
            raise TypeError("Money can only be multiplied by an int quantity")
        return Money(self.amount_minor * qty, self.currency)

    __rmul__ = __mul__

    # Ordering compares only within the same currency.
    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor < other.amount_minor

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor <= other.amount_minor

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor > other.amount_minor

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.amount_minor >= other.amount_minor

    # -- splitting ----------------------------------------------------------- #

    def allocate(self, weights: list[int]) -> list[Money]:
        """Split this amount across integer ``weights`` with no lost minor units.

        The classic "split $1.00 three ways" problem: ``[34, 33, 33]`` cents, not
        ``[33, 33, 33]`` (which loses a cent). Remainder minor units are handed
        out one-at-a-time to the largest weights first (deterministic).
        """
        if not weights:
            raise ValueError("allocate needs at least one weight")
        if any(w < 0 for w in weights):
            raise ValueError("allocate weights must be non-negative")
        total_weight = sum(weights)
        if total_weight == 0:
            raise ValueError("allocate weights must not sum to zero")

        total = self.amount_minor
        # Floor share per weight (toward zero is wrong for negatives; use exact
        # integer division that matches the sign of ``total``).
        shares = [self._floor_share(total, w, total_weight) for w in weights]
        remainder = total - sum(shares)
        step = 1 if remainder >= 0 else -1
        # Distribute the leftover minor units to the heaviest weights first.
        order = sorted(range(len(weights)), key=lambda i: weights[i], reverse=True)
        i = 0
        while remainder != 0:
            shares[order[i % len(order)]] += step
            remainder -= step
            i += 1
        return [Money(s, self.currency) for s in shares]

    @staticmethod
    def _floor_share(total: int, weight: int, total_weight: int) -> int:
        # Truncate toward zero so the remainder loop only ever *adds* magnitude.
        product = total * weight
        share = abs(product) // total_weight
        return share if product >= 0 else -share

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Money({self.amount_minor}, {self.currency!r})"


def apply_rate(amount: Money, rate: Decimal | str | int, *, banker: bool = False) -> Money:
    """Multiply ``amount`` by a ratio ``rate``, rounding to whole minor units.

    Used for tax (``rate=0.085``) and percent proration/discounts. Default
    rounding is HALF_UP (the common invoicing convention); pass ``banker=True``
    for HALF_EVEN where required. ``rate`` is never a float — pass a Decimal/str
    so the ratio itself is exact.
    """
    if isinstance(rate, float):  # pragma: no cover - typing guard
        raise TypeError("pass rate as Decimal/str/int, never float")
    dec_rate = Decimal(rate)
    raw = Decimal(amount.amount_minor) * dec_rate
    rounding = ROUND_HALF_EVEN if banker else ROUND_HALF_UP
    rounded = int(raw.quantize(Decimal(1), rounding=rounding))
    return Money(rounded, amount.currency)


def sum_money(items: list[Money], currency: str = DEFAULT_CURRENCY) -> Money:
    """Sum a (possibly empty) list of same-currency :class:`Money` values."""
    total = Money.zero(currency)
    for item in items:
        total = total + item
    return total


__all__ = [
    "CURRENCY_MINOR_DIGITS",
    "DEFAULT_CURRENCY",
    "Money",
    "apply_rate",
    "minor_digits",
    "minor_per_major",
    "normalize_currency",
    "sum_money",
]
