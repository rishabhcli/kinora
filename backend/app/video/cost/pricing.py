"""A declarative, composable pricing model so heterogeneous providers price on one ruler.

Every provider's published price sheet is expressed as an ordered list of small
:class:`PriceComponent` rules over a :class:`VideoCostRequest`. The components are
*data*, not code paths, so a new provider is a registry entry rather than a new
branch in an estimator:

* :class:`FlatPerClip` — a single charge regardless of length (MiniMax-style).
* :class:`PerSecond` — a rate per second of clip (the Wan / free-tier model).
* :class:`PerResolutionTier` — a per-second rate that varies by resolution.
* :class:`PerFrame` — a rate per frame (``duration_s * fps``).
* :class:`SurgeMultiplier` — scales the running subtotal during peak windows.
* :class:`MinimumCharge` — floors the subtotal (provider minimums).

A provider's :class:`ProviderPricing` also declares an optional **free-tier
quota** (e.g. DashScope's ~1,650 video-seconds): the first N seconds carry the
free rate, the overflow carries the paid rate. The quota is *stateful per
account*, so the estimator is told how much quota is already consumed and prices
only the marginal cost of *this* clip — which is exactly what makes
``cheapest_capable`` correct once the free tier is exhausted on one provider but
not another.

All money is exact :class:`~app.video.cost.money.Money`; quotas are integer
seconds; multipliers are :class:`decimal.Decimal`. Pure, deterministic, no clock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal

from app.video.cost.money import Currency, Money
from app.video.cost.request import VideoCostRequest

# --------------------------------------------------------------------------- #
# Pricing context (what the components are evaluated against)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PricingContext:
    """Inputs a price component sees beyond the request itself.

    ``free_seconds_remaining`` is the provider's *remaining* free-tier quota for
    the billing account at evaluation time (``None`` → the provider has no free
    tier, treat all seconds as paid). Splitting the clip across the quota boundary
    is what makes the marginal price correct.
    """

    currency: Currency
    free_seconds_remaining: int | None = None


# --------------------------------------------------------------------------- #
# Components
# --------------------------------------------------------------------------- #


class PriceComponent(ABC):
    """One additive (or multiplicative) rule contributing to a clip's price."""

    #: A short stable label used in the cost breakdown line items.
    label: str

    @abstractmethod
    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        """Return the new running subtotal after this component.

        Additive components ignore ``subtotal`` and return ``subtotal + charge``;
        multiplicative/flooring components transform ``subtotal`` in place.
        """
        ...


@dataclass(frozen=True, slots=True)
class FlatPerClip(PriceComponent):
    """A single fixed charge per clip regardless of duration (MiniMax 6s @ $0.19)."""

    amount: Money
    label: str = "flat_per_clip"

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        if request.duration_s <= 0:
            return subtotal
        return subtotal + self.amount


@dataclass(frozen=True, slots=True)
class PerSecond(PriceComponent):
    """A rate charged per *paid* second of clip, honoring any free-tier quota.

    ``rate`` is the price for one second. When ``ctx.free_seconds_remaining`` is
    set, the first portion of the clip up to that quota is charged at
    ``free_rate`` (default: free), the remainder at ``rate``.
    """

    rate: Money
    free_rate: Money | None = None
    label: str = "per_second"

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        seconds = request.duration_s
        if seconds <= 0:
            return subtotal
        free_avail = ctx.free_seconds_remaining
        if free_avail is None or free_avail <= 0:
            return subtotal + self.rate.scaled(Decimal(str(seconds)))
        free_used = min(seconds, float(free_avail))
        paid = seconds - free_used
        free_rate = self.free_rate if self.free_rate is not None else Money.zero(ctx.currency)
        charge = free_rate.scaled(Decimal(str(free_used))) + self.rate.scaled(Decimal(str(paid)))
        return subtotal + charge


@dataclass(frozen=True, slots=True)
class PerResolutionTier(PriceComponent):
    """A per-second rate that varies by resolution tier (480P < 720P < 1080P).

    ``rates`` maps an upper-cased tier label to its per-second :class:`Money`.
    Unknown tiers fall back to ``default_rate`` (or the highest declared rate when
    no default is given, so an unrecognized tier is never under-priced). The
    free-tier quota applies to the *seconds*, identically to :class:`PerSecond`.
    """

    rates: dict[str, Money]
    default_rate: Money | None = None
    label: str = "per_resolution_tier"

    def _rate_for(self, tier: str) -> Money:
        if tier in self.rates:
            return self.rates[tier]
        if self.default_rate is not None:
            return self.default_rate
        # Conservative: never under-price an unknown tier.
        return max(self.rates.values())

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        seconds = request.duration_s
        if seconds <= 0:
            return subtotal
        rate = self._rate_for(request.resolution_tier)
        free_avail = ctx.free_seconds_remaining
        if free_avail is None or free_avail <= 0:
            return subtotal + rate.scaled(Decimal(str(seconds)))
        free_used = min(seconds, float(free_avail))
        paid = seconds - free_used
        return subtotal + rate.scaled(Decimal(str(paid)))


@dataclass(frozen=True, slots=True)
class PerFrame(PriceComponent):
    """A rate charged per frame (``round(duration_s * fps)``)."""

    rate: Money
    label: str = "per_frame"

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        frames = request.frame_count
        if frames <= 0:
            return subtotal
        return subtotal + self.rate * frames


@dataclass(frozen=True, slots=True)
class SurgeMultiplier(PriceComponent):
    """Scale the running subtotal by ``multiplier`` when ``request.peak`` is set.

    The estimator never reads a clock — the *caller* marks a request ``peak`` —
    so this stays a pure function of its inputs.
    """

    multiplier: Decimal
    label: str = "surge"

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        if not request.peak or self.multiplier == Decimal(1):
            return subtotal
        return subtotal.scaled(self.multiplier)


@dataclass(frozen=True, slots=True)
class PriorityMultiplier(PriceComponent):
    """Scale the subtotal by a per-priority-lane factor (e.g. a rush surcharge)."""

    multipliers: dict[str, Decimal]
    label: str = "priority"

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        factor = self.multipliers.get(request.priority)
        if factor is None or factor == Decimal(1):
            return subtotal
        return subtotal.scaled(factor)


@dataclass(frozen=True, slots=True)
class MinimumCharge(PriceComponent):
    """Floor the subtotal at ``minimum`` (provider minimum charge per call)."""

    minimum: Money
    label: str = "minimum_charge"

    def apply(self, request: VideoCostRequest, subtotal: Money, ctx: PricingContext) -> Money:
        if request.duration_s <= 0:
            return subtotal
        return Money.max(subtotal, self.minimum)


# --------------------------------------------------------------------------- #
# Per-provider pricing + the registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProviderPricing:
    """The full declarative price sheet for one provider/model.

    Attributes:
        provider: The provider id (matches the router's backend ``name`` family).
        model: The specific model id the sheet prices.
        currency: The currency every component is denominated in.
        components: Ordered rules. Additive rules accumulate; multiplicative /
            flooring rules transform the subtotal, so **order matters** (surge
            before minimum-charge floors the surged price, etc.).
        free_tier_seconds: Total free video-seconds for the account (``None`` →
            no free tier). The estimator subtracts already-consumed quota.
        confidence: A 0..1 self-rating of how exactly this sheet matches the
            provider's real bill (published & verified → 1.0; estimated → lower),
            surfaced on the estimate so the router can prefer well-characterized
            providers when prices tie.
        notes: Free-text provenance for the price (doc link / date).
    """

    provider: str
    model: str
    currency: Currency
    components: tuple[PriceComponent, ...]
    free_tier_seconds: int | None = None
    confidence: float = 1.0
    notes: str = ""

    def price(self, request: VideoCostRequest, *, free_seconds_consumed: int = 0) -> Money:
        """Compute the *marginal* exact price of ``request`` for this provider.

        ``free_seconds_consumed`` is how much of the free tier is already gone for
        the billing account; the marginal cost prices only the unfunded remainder.
        """
        remaining: int | None
        if self.free_tier_seconds is None:
            remaining = None
        else:
            remaining = max(0, self.free_tier_seconds - max(0, free_seconds_consumed))
        ctx = PricingContext(currency=self.currency, free_seconds_remaining=remaining)
        subtotal = Money.zero(self.currency)
        for component in self.components:
            subtotal = component.apply(request, subtotal, ctx)
        return subtotal

    def line_items(
        self, request: VideoCostRequest, *, free_seconds_consumed: int = 0
    ) -> list[tuple[str, Money]]:
        """Return ``(label, running_subtotal)`` after each component (for breakdowns)."""
        remaining: int | None
        if self.free_tier_seconds is None:
            remaining = None
        else:
            remaining = max(0, self.free_tier_seconds - max(0, free_seconds_consumed))
        ctx = PricingContext(currency=self.currency, free_seconds_remaining=remaining)
        items: list[tuple[str, Money]] = []
        subtotal = Money.zero(self.currency)
        for component in self.components:
            subtotal = component.apply(request, subtotal, ctx)
            items.append((component.label, subtotal))
        return items


class PricingRegistry:
    """A mutable lookup of :class:`ProviderPricing` keyed by ``(provider, model)``.

    The registry is the single place new providers are declared, so the estimator
    and ``cheapest_capable`` enumerate exactly the same set. Keys are matched
    case-insensitively on the provider id; a provider may register several models.
    """

    def __init__(self, sheets: list[ProviderPricing] | None = None) -> None:
        self._sheets: dict[tuple[str, str], ProviderPricing] = {}
        for sheet in sheets or []:
            self.register(sheet)

    def register(self, sheet: ProviderPricing) -> None:
        self._sheets[(sheet.provider.lower(), sheet.model)] = sheet

    def get(self, provider: str, model: str) -> ProviderPricing:
        try:
            return self._sheets[(provider.lower(), model)]
        except KeyError as exc:
            raise KeyError(f"no pricing registered for {provider!r}/{model!r}") from exc

    def get_default(self, provider: str) -> ProviderPricing:
        """Return the single sheet for ``provider`` (errors if 0 or >1 models)."""
        matches = [s for (p, _), s in self._sheets.items() if p == provider.lower()]
        if len(matches) != 1:
            raise KeyError(
                f"expected exactly one model for provider {provider!r}, found {len(matches)}"
            )
        return matches[0]

    def has(self, provider: str, model: str) -> bool:
        return (provider.lower(), model) in self._sheets

    def sheets(self) -> list[ProviderPricing]:
        return list(self._sheets.values())

    def providers(self) -> set[str]:
        return {p for (p, _) in self._sheets}


# --------------------------------------------------------------------------- #
# A default registry seeded from kinora.md §11 / config defaults
# --------------------------------------------------------------------------- #

#: Wan free tier ≈ 1,650 video-seconds (kinora.md §11.1). The paid per-second rate
#: is a representative published figure used only when the free tier is exhausted;
#: it is intentionally conservative and tagged with lower confidence than MiniMax's
#: directly-published per-clip price.
_WAN_FREE_SECONDS = 1650
_WAN_PER_SECOND_PAID = Money.usd("0.10")  # representative overflow rate


def default_registry() -> PricingRegistry:
    """A registry seeded with the providers Kinora actually routes to.

    * ``dashscope`` (Wan turbo): free up to ~1,650s, then per-second; resolution
      tiers reflect 480/720/1080 surcharges.
    * ``minimax`` (Hailuo Fast): a flat per-clip charge ($0.19 @ 768P/6s) — its
      published model, with a minimum charge equal to that flat price.

    Values mirror ``app.core.config.Settings`` defaults; a deployment can build a
    bespoke registry instead (this is only the convenient default).
    """
    wan = ProviderPricing(
        provider="dashscope",
        model="wan2.1-t2v-turbo",
        currency=Currency.USD,
        free_tier_seconds=_WAN_FREE_SECONDS,
        confidence=0.7,
        notes="kinora.md §11.1 free tier ~1650s; overflow rate representative",
        components=(
            PerResolutionTier(
                rates={
                    "480P": Money.usd("0.05"),
                    "720P": _WAN_PER_SECOND_PAID,
                    "768P": _WAN_PER_SECOND_PAID,
                    "1080P": Money.usd("0.20"),
                },
                default_rate=_WAN_PER_SECOND_PAID,
            ),
            SurgeMultiplier(multiplier=Decimal("1.0")),
        ),
    )
    minimax = ProviderPricing(
        provider="minimax",
        model="MiniMax-Hailuo-2.3-Fast",
        currency=Currency.USD,
        free_tier_seconds=None,
        confidence=1.0,
        notes="published $0.19/clip @ 768P/6s (config default)",
        components=(
            FlatPerClip(amount=Money.usd("0.19")),
            MinimumCharge(minimum=Money.usd("0.19")),
        ),
    )
    return PricingRegistry([wan, minimax])


__all__ = [
    "FlatPerClip",
    "MinimumCharge",
    "PerFrame",
    "PerResolutionTier",
    "PerSecond",
    "PriceComponent",
    "PricingContext",
    "PricingRegistry",
    "PriorityMultiplier",
    "ProviderPricing",
    "SurgeMultiplier",
    "default_registry",
]
