"""``CostEstimator`` — predict the marginal cost of a render on a given provider.

Pricing is exact (the components compute one number), but the *prediction* is
not: a provider can clamp the requested duration to a billed minimum, round
seconds up to whole units, or apply a surge the caller couldn't foresee. So an
estimate is a small interval — ``low <= expected <= high`` — plus a confidence
inherited from the provider's price sheet. The router uses ``expected`` to rank
and ``high`` to stay safely under a hard cap (never over-commit on an optimistic
point estimate).

The estimator is told how much of each provider's free tier is already consumed
(via a :class:`QuotaView`) so the marginal price is correct once one provider's
free seconds are gone but another's are not. It holds no state and reads no
clock; the same inputs always yield the same estimate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.video.cost.money import Currency, Money
from app.video.cost.pricing import PricingRegistry, ProviderPricing
from app.video.cost.request import VideoCostRequest


class QuotaView(Protocol):
    """A read-only view of how much free-tier quota a provider has consumed."""

    def free_seconds_consumed(self, provider: str) -> int:
        """Free video-seconds already used on ``provider`` (0 if none/unknown)."""
        ...


@dataclass(frozen=True, slots=True)
class StaticQuotaView:
    """A fixed :class:`QuotaView` from a ``provider -> consumed_seconds`` mapping."""

    consumed: Mapping[str, int]

    def free_seconds_consumed(self, provider: str) -> int:
        return int(self.consumed.get(provider, 0))


#: A :class:`QuotaView` where every provider's free tier is fully intact.
class _ZeroQuota:
    def free_seconds_consumed(self, provider: str) -> int:  # noqa: D401 - trivial
        return 0


ZERO_QUOTA: QuotaView = _ZeroQuota()


@dataclass(frozen=True, slots=True)
class CostEstimate:
    """A predicted marginal cost with an uncertainty band and provenance.

    Attributes:
        provider / model: What was priced.
        expected: The point estimate (the price sheet's exact result).
        low / high: The uncertainty band. ``high`` is what enforcement checks
            against a hard cap; ``low`` is the optimistic floor.
        confidence: 0..1, inherited from the price sheet (1.0 = published/verified).
        free_seconds_applied: Free-tier seconds this clip consumed (0 when paid).
        line_items: ``(label, running_subtotal)`` after each component, for UI/audit.
    """

    provider: str
    model: str
    expected: Money
    low: Money
    high: Money
    confidence: float
    free_seconds_applied: float
    line_items: tuple[tuple[str, Money], ...] = ()

    @property
    def currency(self) -> Currency:
        return self.expected.currency

    def as_log_fields(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "model": self.model,
            "expected": str(self.expected.to_decimal()),
            "low": str(self.low.to_decimal()),
            "high": str(self.high.to_decimal()),
            "currency": self.currency.value,
            "confidence": round(self.confidence, 3),
        }


class CostEstimator:
    """Predict per-provider marginal cost from the declarative pricing model.

    ``band_fraction`` widens the ``[low, high]`` interval around the exact price
    in inverse proportion to the sheet's confidence: a fully-confident sheet gets
    a tight band, an estimated sheet a wide one. The widening is a deterministic
    function of confidence, never random.
    """

    def __init__(
        self,
        registry: PricingRegistry,
        *,
        band_fraction: Decimal = Decimal("0.15"),
    ) -> None:
        self._registry = registry
        self._band_fraction = band_fraction

    @property
    def registry(self) -> PricingRegistry:
        return self._registry

    def _band(self, expected: Money, confidence: float) -> tuple[Money, Money]:
        # A confident sheet (1.0) → zero extra band; an unsure sheet → up to
        # ``band_fraction`` of the price on each side. Deterministic, monotone.
        spread = self._band_fraction * (Decimal(1) - Decimal(str(max(0.0, min(1.0, confidence)))))
        if spread <= 0:
            return expected, expected
        delta = expected.scaled(spread)
        return expected - delta, expected + delta

    def _estimate_sheet(
        self, sheet: ProviderPricing, request: VideoCostRequest, *, free_consumed: int
    ) -> CostEstimate:
        expected = sheet.price(request, free_seconds_consumed=free_consumed)
        low, high = self._band(expected, sheet.confidence)
        free_applied = 0.0
        if sheet.free_tier_seconds is not None:
            remaining = max(0, sheet.free_tier_seconds - max(0, free_consumed))
            free_applied = min(request.duration_s, float(remaining))
        return CostEstimate(
            provider=sheet.provider,
            model=sheet.model,
            expected=expected,
            low=low,
            high=high,
            confidence=sheet.confidence,
            free_seconds_applied=free_applied,
            line_items=tuple(sheet.line_items(request, free_seconds_consumed=free_consumed)),
        )

    def estimate(
        self,
        request: VideoCostRequest,
        provider: str,
        model: str,
        *,
        quota: QuotaView = ZERO_QUOTA,
    ) -> CostEstimate:
        """Estimate the marginal cost of ``request`` on ``provider``/``model``."""
        sheet = self._registry.get(provider, model)
        return self._estimate_sheet(
            sheet, request, free_consumed=quota.free_seconds_consumed(provider)
        )

    def estimate_all(
        self,
        request: VideoCostRequest,
        *,
        quota: QuotaView = ZERO_QUOTA,
    ) -> list[CostEstimate]:
        """Estimate ``request`` across every registered provider/model.

        Sorted ascending by ``expected`` then by descending confidence, so the
        first element is the cheapest best-characterized option — a sensible
        default before any capability/cap filtering.
        """
        out = [
            self._estimate_sheet(
                sheet, request, free_consumed=quota.free_seconds_consumed(sheet.provider)
            )
            for sheet in self._registry.sheets()
        ]
        out.sort(key=lambda e: (e.expected.units, -e.confidence))
        return out


__all__ = [
    "ZERO_QUOTA",
    "CostEstimate",
    "CostEstimator",
    "QuotaView",
    "StaticQuotaView",
]
