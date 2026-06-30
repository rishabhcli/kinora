"""Unit tests for the declarative pricing model + estimator.

Covers every component kind (flat per-clip, per-second, per-resolution tier,
per-frame, surge, priority, minimum charge), the free-tier quota split (the
clip straddling the boundary), the registry, and the confidence-scaled estimate
band. Pure logic, no infra.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.video.cost.estimator import CostEstimator, StaticQuotaView
from app.video.cost.money import Currency, Money
from app.video.cost.pricing import (
    FlatPerClip,
    MinimumCharge,
    PerFrame,
    PerResolutionTier,
    PerSecond,
    PricingRegistry,
    PriorityMultiplier,
    ProviderPricing,
    SurgeMultiplier,
    default_registry,
)
from app.video.cost.request import VideoCostRequest, VideoMode


def _sheet(*components: object, free: int | None = None, conf: float = 1.0) -> ProviderPricing:
    return ProviderPricing(
        provider="p",
        model="m",
        currency=Currency.USD,
        components=tuple(components),  # type: ignore[arg-type]
        free_tier_seconds=free,
        confidence=conf,
    )


def test_flat_per_clip_ignores_duration() -> None:
    sheet = _sheet(FlatPerClip(amount=Money.usd("0.19")))
    assert sheet.price(VideoCostRequest(duration_s=6)) == Money.usd("0.19")
    assert sheet.price(VideoCostRequest(duration_s=15)) == Money.usd("0.19")
    # Zero-duration clip is not billed.
    assert sheet.price(VideoCostRequest(duration_s=0)) == Money.usd("0")


def test_per_second_no_free_tier() -> None:
    sheet = _sheet(PerSecond(rate=Money.usd("0.10")))
    assert sheet.price(VideoCostRequest(duration_s=5)) == Money.usd("0.50")


def test_per_second_free_tier_partial_straddle() -> None:
    # Free for 4 of the 6 seconds, paid for the remaining 2 @ 0.10 = 0.20.
    sheet = _sheet(PerSecond(rate=Money.usd("0.10")), free=1650)
    req = VideoCostRequest(duration_s=6)
    # 1648 already consumed -> only 2 free seconds left.
    assert sheet.price(req, free_seconds_consumed=1648) == Money.usd("0.40")
    # 1644 consumed -> 6 free left -> whole clip free.
    assert sheet.price(req, free_seconds_consumed=1644) == Money.usd("0")
    # Fully exhausted -> whole clip paid.
    assert sheet.price(req, free_seconds_consumed=1650) == Money.usd("0.60")


def test_per_resolution_tier_picks_rate_and_default() -> None:
    sheet = _sheet(
        PerResolutionTier(
            rates={"480P": Money.usd("0.05"), "1080P": Money.usd("0.20")},
            default_rate=Money.usd("0.10"),
        )
    )
    assert sheet.price(VideoCostRequest(duration_s=10, resolution="480P")) == Money.usd("0.50")
    assert sheet.price(VideoCostRequest(duration_s=10, resolution="1080P")) == Money.usd("2.00")
    # Unknown tier -> default rate.
    assert sheet.price(VideoCostRequest(duration_s=10, resolution="999P")) == Money.usd("1.00")


def test_per_resolution_tier_unknown_without_default_is_conservative() -> None:
    sheet = _sheet(
        PerResolutionTier(rates={"480P": Money.usd("0.05"), "1080P": Money.usd("0.20")})
    )
    # No default -> never under-price -> uses the max rate.
    assert sheet.price(VideoCostRequest(duration_s=1, resolution="4K")) == Money.usd("0.20")


def test_per_frame() -> None:
    sheet = _sheet(PerFrame(rate=Money.usd("0.001")))
    # 2s @ 24fps = 48 frames * 0.001 = 0.048.
    assert sheet.price(VideoCostRequest(duration_s=2, fps=24)) == Money.usd("0.048")


def test_surge_multiplier_only_on_peak() -> None:
    sheet = _sheet(
        FlatPerClip(amount=Money.usd("0.20")), SurgeMultiplier(multiplier=Decimal("1.5"))
    )
    assert sheet.price(VideoCostRequest(duration_s=6, peak=False)) == Money.usd("0.20")
    assert sheet.price(VideoCostRequest(duration_s=6, peak=True)) == Money.usd("0.30")


def test_priority_multiplier() -> None:
    sheet = _sheet(
        FlatPerClip(amount=Money.usd("0.20")),
        PriorityMultiplier(multipliers={"rush": Decimal("2.0")}),
    )
    assert sheet.price(VideoCostRequest(duration_s=6, priority="committed")) == Money.usd("0.20")
    assert sheet.price(VideoCostRequest(duration_s=6, priority="rush")) == Money.usd("0.40")


def test_minimum_charge_floors_subtotal() -> None:
    sheet = _sheet(PerSecond(rate=Money.usd("0.01")), MinimumCharge(minimum=Money.usd("0.19")))
    # 2s @ 0.01 = 0.02, floored to 0.19.
    assert sheet.price(VideoCostRequest(duration_s=2)) == Money.usd("0.19")
    # 30s @ 0.01 = 0.30 stays above the floor.
    assert sheet.price(VideoCostRequest(duration_s=30)) == Money.usd("0.30")


def test_component_order_surge_before_minimum() -> None:
    # Surge then minimum: surge the per-second subtotal, then floor.
    sheet = _sheet(
        PerSecond(rate=Money.usd("0.01")),
        SurgeMultiplier(multiplier=Decimal("2.0")),
        MinimumCharge(minimum=Money.usd("0.05")),
    )
    # 2s -> 0.02 -> surge 0.04 -> floored to 0.05.
    assert sheet.price(VideoCostRequest(duration_s=2, peak=True)) == Money.usd("0.05")


def test_line_items_running_subtotals() -> None:
    sheet = _sheet(
        PerSecond(rate=Money.usd("0.10")),
        SurgeMultiplier(multiplier=Decimal("1.5")),
    )
    items = sheet.line_items(VideoCostRequest(duration_s=4, peak=True))
    assert [label for label, _ in items] == ["per_second", "surge"]
    assert items[0][1] == Money.usd("0.40")
    assert items[1][1] == Money.usd("0.60")


def test_registry_lookup_and_defaults() -> None:
    reg = default_registry()
    assert reg.has("minimax", "MiniMax-Hailuo-2.3-Fast")
    assert reg.has("DASHSCOPE", "wan2.1-t2v-turbo")  # case-insensitive provider
    with pytest.raises(KeyError):
        reg.get("nope", "x")
    assert reg.providers() == {"dashscope", "minimax"}


def test_registry_get_default_single_model() -> None:
    reg = PricingRegistry([_sheet(FlatPerClip(amount=Money.usd("0.19")))])
    assert reg.get_default("p").model == "m"
    reg.register(ProviderPricing("p", "m2", Currency.USD, ()))
    with pytest.raises(KeyError):
        reg.get_default("p")  # ambiguous now


def test_estimator_band_scales_with_confidence() -> None:
    one = (FlatPerClip(amount=Money.usd("1.00")),)
    reg = PricingRegistry(
        [
            ProviderPricing("hi", "m", Currency.USD, one, confidence=1.0),
            ProviderPricing("lo", "m", Currency.USD, one, confidence=0.0),
        ]
    )
    est = CostEstimator(reg, band_fraction=Decimal("0.20"))
    req = VideoCostRequest(duration_s=6)
    hi = est.estimate(req, "hi", "m")
    lo = est.estimate(req, "lo", "m")
    # Full confidence -> zero band.
    assert hi.low == hi.expected == hi.high == Money.usd("1.00")
    # Zero confidence -> ±20% band.
    assert lo.low == Money.usd("0.80")
    assert lo.high == Money.usd("1.20")


def test_estimate_all_sorted_cheapest_first() -> None:
    est = CostEstimator(default_registry())
    # Exhaust the Wan free tier so it is the expensive option.
    quota = StaticQuotaView({"dashscope": 1650})
    req = VideoCostRequest(duration_s=6, resolution="720P", mode=VideoMode.TEXT_TO_VIDEO)
    ranked = est.estimate_all(req, quota=quota)
    assert ranked[0].provider == "minimax"  # 0.19 < 0.60
    assert ranked[0].expected == Money.usd("0.19")
    assert ranked[-1].provider == "dashscope"


def test_estimate_free_seconds_applied_reported() -> None:
    est = CostEstimator(default_registry())
    req = VideoCostRequest(duration_s=6, resolution="720P")
    e = est.estimate(req, "dashscope", "wan2.1-t2v-turbo")
    assert e.free_seconds_applied == 6.0
    assert e.expected == Money.usd("0")
