"""A multi-currency money type stored as integer *minor units* — no float drift.

Heterogeneous video providers quote prices in dollars, cents, fractional cents
(per-second tiers), and even free-tier credits. If every one of those were a
Python ``float`` the cross-provider comparison this subsystem exists to make
("which provider is cheapest for *this* clip?") would be polluted by binary
rounding: ``0.1 + 0.2 != 0.3`` is exactly the kind of error that, summed over a
1,650-second budget, silently drifts a hard USD ceiling.

So :class:`Money` is an **integer** count of the smallest representable unit of a
currency, scaled by :data:`MINOR_UNIT_SCALE` past the currency's natural minor
unit so sub-cent per-second pricing stays exact. All arithmetic is integer
arithmetic; the only place a float ever appears is at the trust boundary
(:meth:`Money.from_decimal` / :meth:`Money.to_decimal`), and even there the
conversion goes through :class:`decimal.Decimal` with explicit rounding so the
rounding mode is a choice, never an accident.

Design rules:

* **Same-currency arithmetic only.** Adding USD to EUR raises
  :class:`CurrencyMismatch` — there is no implicit FX. A :class:`FxConverter`
  makes conversion an *explicit*, auditable step.
* **Exact construction from human prices.** ``Money.usd("0.19")`` is exact;
  ``Money.usd(0.19)`` is rejected (pass a ``str``/``Decimal``, or use
  :meth:`from_float` and accept the documented rounding).
* **Banker's-rounding scaling.** When a price has more precision than the scale
  allows (e.g. a per-fps fraction), the excess is rounded half-to-even.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from enum import StrEnum
from typing import Final

#: Extra decimal places kept *beyond* a currency's natural minor unit. A USD cent
#: scaled by ``10**4`` becomes 10,000 internal units, so a per-second price like
#: $0.0317/s is representable exactly. Chosen once, globally, so all currencies
#: share one internal granularity and integer totals are directly comparable
#: after FX has normalized the currency.
MINOR_UNIT_SCALE: Final[int] = 4


class Currency(StrEnum):
    """ISO-4217 currencies this subsystem prices in.

    ``minor_digits`` is the number of fractional digits in the currency's natural
    minor unit (USD/EUR cents → 2; JPY → 0). The internal integer of a
    :class:`Money` is ``value * 10**(minor_digits + MINOR_UNIT_SCALE)``.
    """

    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    CNY = "CNY"  # DashScope / Alibaba bills in RMB
    JPY = "JPY"

    @property
    def minor_digits(self) -> int:
        return 0 if self is Currency.JPY else 2

    @property
    def _internal_exponent(self) -> int:
        return self.minor_digits + MINOR_UNIT_SCALE


class CurrencyMismatch(ValueError):  # noqa: N818 - public, intentional non-Error name
    """Raised on arithmetic between two different currencies (no implicit FX)."""

    def __init__(self, left: Currency, right: Currency) -> None:
        self.left = left
        self.right = right
        super().__init__(f"cannot combine {left} and {right} without explicit FX")


@dataclass(frozen=True, slots=True, order=False)
class Money:
    """An exact monetary amount: ``units`` smallest-internal-units of ``currency``.

    ``units`` is signed (a negative balance / refund is representable). Ordering
    and equality are defined *within a currency only*; comparing across
    currencies raises rather than silently lying.
    """

    units: int
    currency: Currency

    # -- construction ----------------------------------------------------- #

    @classmethod
    def zero(cls, currency: Currency = Currency.USD) -> Money:
        return cls(0, currency)

    @classmethod
    def from_decimal(cls, amount: Decimal | str | int, currency: Currency) -> Money:
        """Exact construction from a decimal string / :class:`Decimal` / int.

        Excess precision beyond the internal scale is rounded half-to-even. A
        ``float`` is rejected here on purpose — route it through
        :meth:`from_float` so the lossy step is explicit at the call site.
        """
        if isinstance(amount, float):  # pragma: no cover - guarded by type, kept defensive
            raise TypeError("pass a str/Decimal/int to from_decimal (use from_float for floats)")
        dec = amount if isinstance(amount, Decimal) else Decimal(amount)
        scale = Decimal(10) ** currency._internal_exponent
        with localcontext() as ctx:
            ctx.prec = 60
            scaled = (dec * scale).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
        return cls(int(scaled), currency)

    @classmethod
    def from_float(cls, amount: float, currency: Currency) -> Money:
        """Construct from a ``float`` via :class:`Decimal`, rounding half-to-even.

        Use only when the source is genuinely a float (a provider SDK returning
        ``0.19``). For literals prefer :meth:`from_decimal` with a string.
        """
        return cls.from_decimal(Decimal(str(amount)), currency)

    @classmethod
    def usd(cls, amount: Decimal | str | int) -> Money:
        """Shorthand for an exact USD amount from a decimal string/Decimal/int."""
        return cls.from_decimal(amount, Currency.USD)

    @classmethod
    def from_minor(cls, minor_units: int, currency: Currency) -> Money:
        """Construct from the currency's *natural* minor units (e.g. cents)."""
        return cls(int(minor_units) * (10**MINOR_UNIT_SCALE), currency)

    # -- conversion (trust boundary) -------------------------------------- #

    def to_decimal(self) -> Decimal:
        """Exact major-unit value (e.g. ``Decimal('0.1900')`` for 19c USD)."""
        scale = Decimal(10) ** self.currency._internal_exponent
        return Decimal(self.units) / scale

    def to_float(self) -> float:
        """Lossy major-unit value as a ``float`` (display / legacy bridges only)."""
        return float(self.to_decimal())

    def to_minor(self) -> int:
        """Value in the currency's natural minor unit, rounded half-to-even.

        (Used when bridging to the legacy ``SpendStore`` whose unit is dollars and
        whose internal counter is a float; here we keep the integer cent count.)
        """
        scale = Decimal(10) ** MINOR_UNIT_SCALE
        with localcontext() as ctx:
            ctx.prec = 60
            return int((Decimal(self.units) / scale).quantize(Decimal(1), ROUND_HALF_EVEN))

    # -- arithmetic (integer, same-currency) ------------------------------ #

    def _check(self, other: Money) -> None:
        if self.currency is not other.currency:
            raise CurrencyMismatch(self.currency, other.currency)

    def __add__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.units + other.units, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._check(other)
        return Money(self.units - other.units, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.units, self.currency)

    def __mul__(self, factor: int) -> Money:
        """Scale by an integer count (e.g. N clips). Floats are rejected — scale a
        *price* by a fractional quantity through :meth:`scaled` instead, so the
        rounding is explicit."""
        if not isinstance(factor, int) or isinstance(factor, bool):
            raise TypeError("Money * count requires an int (use .scaled for fractions)")
        return Money(self.units * factor, self.currency)

    __rmul__ = __mul__

    def scaled(self, factor: Decimal | str | int) -> Money:
        """Multiply by a (possibly fractional) factor, rounding half-to-even.

        For e.g. a surge multiplier (``1.5``) or a per-second price times a
        non-integer duration. The factor is taken as a :class:`Decimal` so the
        product is exact before the single, explicit rounding back to units.
        """
        dec = factor if isinstance(factor, Decimal) else Decimal(str(factor))
        with localcontext() as ctx:
            ctx.prec = 60
            scaled = (Decimal(self.units) * dec).quantize(Decimal(1), ROUND_HALF_EVEN)
        return Money(int(scaled), self.currency)

    # -- predicates / ordering (within a currency) ------------------------ #

    def is_zero(self) -> bool:
        return self.units == 0

    def is_positive(self) -> bool:
        return self.units > 0

    def __lt__(self, other: Money) -> bool:
        self._check(other)
        return self.units < other.units

    def __le__(self, other: Money) -> bool:
        self._check(other)
        return self.units <= other.units

    def __gt__(self, other: Money) -> bool:
        self._check(other)
        return self.units > other.units

    def __ge__(self, other: Money) -> bool:
        self._check(other)
        return self.units >= other.units

    @staticmethod
    def max(a: Money, b: Money) -> Money:
        return a if a >= b else b

    @staticmethod
    def min(a: Money, b: Money) -> Money:
        return a if a <= b else b

    # -- rendering -------------------------------------------------------- #

    def __str__(self) -> str:
        digits = self.currency.minor_digits
        return f"{self.to_decimal():.{digits}f} {self.currency.value}"

    def as_log_fields(self) -> dict[str, object]:
        """Structured-log-safe representation (exact string + currency)."""
        return {"amount": str(self.to_decimal()), "currency": self.currency.value}


@dataclass(frozen=True, slots=True)
class FxConverter:
    """An explicit, auditable foreign-exchange step between currencies.

    Rates are major-unit-per-unit-of-base (``rates[EUR] = "0.92"`` means 1 USD =
    0.92 EUR when ``base`` is USD). Conversion is exact via :class:`Decimal` and
    rounds once at the end. There is intentionally *no* implicit conversion inside
    :class:`Money` — you must reach for this type, which makes every cross-currency
    comparison visible in the call graph (and in tests).
    """

    base: Currency
    rates: dict[Currency, Decimal]

    @classmethod
    def from_rate_strings(cls, base: Currency, rates: dict[Currency, str]) -> FxConverter:
        return cls(base=base, rates={c: Decimal(v) for c, v in rates.items()})

    def _rate(self, currency: Currency) -> Decimal:
        if currency is self.base:
            return Decimal(1)
        try:
            return self.rates[currency]
        except KeyError as exc:  # pragma: no cover - exercised via convert()
            raise KeyError(f"no FX rate for {currency} from base {self.base}") from exc

    def convert(self, money: Money, to: Currency) -> Money:
        """Convert ``money`` to currency ``to`` via the base, exact then rounded."""
        if money.currency is to:
            return money
        # value_in_base = money / rate(money.currency); target = value_in_base * rate(to)
        major = money.to_decimal()
        base_value = major / self._rate(money.currency)
        target_value = base_value * self._rate(to)
        return Money.from_decimal(target_value, to)


__all__ = [
    "MINOR_UNIT_SCALE",
    "Currency",
    "CurrencyMismatch",
    "FxConverter",
    "Money",
]
