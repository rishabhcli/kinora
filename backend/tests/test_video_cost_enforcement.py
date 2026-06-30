"""Unit tests for layered money-cap enforcement, race safety, and cheapest_capable.

Covers: global / per-provider / per-book caps each binding independently with the
right typed BudgetExceeded scope; the soft-cap flag; the reservation race (two
concurrent reserves that together breach a cap — at most one succeeds); and
cheapest_capable ranking on the worst-case (high) estimate, free-tier-aware, with
typed failure when nothing fits.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.video.cost.enforcement import (
    BudgetCaps,
    BudgetEnforcer,
    BudgetExceeded,
    CapabilityCandidate,
    cheapest_capable,
)
from app.video.cost.estimator import CostEstimator, StaticQuotaView
from app.video.cost.ledger import InMemorySpendLedger, SpendScope
from app.video.cost.money import Currency, CurrencyMismatch, Money
from app.video.cost.pricing import (
    FlatPerClip,
    PricingRegistry,
    ProviderPricing,
)
from app.video.cost.request import VideoCostRequest


def _enforcer(caps: BudgetCaps) -> BudgetEnforcer:
    return BudgetEnforcer(InMemorySpendLedger(), caps)


def _req() -> VideoCostRequest:
    return VideoCostRequest(duration_s=6)


async def test_global_cap_binds() -> None:
    enf = _enforcer(BudgetCaps.usd(Money.usd("0.30")))
    scope = SpendScope(provider="minimax")
    await enf.reserve(Money.usd("0.19"), scope)
    with pytest.raises(BudgetExceeded) as ei:
        await enf.reserve(Money.usd("0.19"), scope)  # 0.38 > 0.30
    assert ei.value.scope == "global"
    assert ei.value.cap == Money.usd("0.30")


async def test_per_provider_cap_binds_before_global() -> None:
    caps = BudgetCaps.usd(
        Money.usd("30.00"), per_provider={"minimax": Money.usd("0.30")}
    )
    enf = _enforcer(caps)
    await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax"))
    # dashscope is free under its (absent) cap and the global cap.
    await enf.reserve(Money.usd("5.00"), SpendScope(provider="dashscope"))
    with pytest.raises(BudgetExceeded) as ei:
        await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax"))
    assert ei.value.scope == "provider:minimax"


async def test_per_book_cap_binds() -> None:
    caps = BudgetCaps.usd(Money.usd("30.00"), per_book=Money.usd("0.30"))
    enf = _enforcer(caps)
    await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b1"))
    # A different book is unaffected.
    await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b2"))
    with pytest.raises(BudgetExceeded) as ei:
        await enf.reserve(Money.usd("0.19"), SpendScope(provider="dashscope", book_id="b1"))
    assert ei.value.scope == "book:b1"


async def test_per_book_counts_reserved_and_committed() -> None:
    caps = BudgetCaps.usd(Money.usd("30.00"), per_book=Money.usd("0.40"))
    enf = _enforcer(caps)
    r1 = await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b1"))
    await enf.commit(r1)  # now committed, still counts against the book
    await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b1"))
    with pytest.raises(BudgetExceeded):
        await enf.reserve(Money.usd("0.19"), SpendScope(provider="minimax", book_id="b1"))


async def test_release_frees_room_under_cap() -> None:
    enf = _enforcer(BudgetCaps.usd(Money.usd("0.30")))
    scope = SpendScope(provider="minimax")
    r1 = await enf.reserve(Money.usd("0.19"), scope)
    await enf.release(r1)
    # Room is back.
    await enf.reserve(Money.usd("0.19"), scope)
    assert (await enf.remaining_global()) == Money.usd("0.11")


async def test_soft_cap_flag_without_blocking() -> None:
    caps = BudgetCaps.usd(Money.usd("1.00"), soft_cap_fraction=Decimal("0.50"))
    enf = _enforcer(caps)
    # 0.60 < hard 1.00 but > soft 0.50 -> affordable, soft_exceeded True.
    check = await enf.can_afford(Money.usd("0.60"), SpendScope(provider="minimax"))
    assert check.affordable and check.soft_exceeded
    # Under the soft cap -> not flagged.
    check2 = await enf.can_afford(Money.usd("0.40"), SpendScope(provider="minimax"))
    assert check2.affordable and not check2.soft_exceeded


async def test_currency_mismatch_in_enforcer() -> None:
    enf = _enforcer(BudgetCaps.usd(Money.usd("1.00")))
    with pytest.raises(CurrencyMismatch):
        await enf.can_afford(Money.from_decimal("1", Currency.EUR), SpendScope(provider="x"))


async def test_reservation_race_at_most_one_wins() -> None:
    # The cap fits exactly one 0.19 reservation; fire many concurrently.
    enf = _enforcer(BudgetCaps.usd(Money.usd("0.30")))
    scope = SpendScope(provider="minimax")

    async def attempt() -> bool:
        try:
            await enf.reserve(Money.usd("0.19"), scope)
            return True
        except BudgetExceeded:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(20)))
    assert sum(results) == 1  # exactly one earmark fit
    assert (await enf.ledger.outstanding()) == Money.usd("0.19")


async def test_reservation_race_two_caps_total_fits_one() -> None:
    # Global cap fits 2 clips, per-provider cap fits 1 -> only 1 minimax wins.
    caps = BudgetCaps.usd(
        Money.usd("1.00"), per_provider={"minimax": Money.usd("0.20")}
    )
    enf = _enforcer(caps)
    scope = SpendScope(provider="minimax")

    async def attempt() -> bool:
        try:
            await enf.reserve(Money.usd("0.19"), scope)
            return True
        except BudgetExceeded:
            return False

    results = await asyncio.gather(*(attempt() for _ in range(10)))
    assert sum(results) == 1


# --------------------------------------------------------------------------- #
# cheapest_capable
# --------------------------------------------------------------------------- #


def _two_provider_registry() -> PricingRegistry:
    return PricingRegistry(
        [
            ProviderPricing(
                "cheap", "m", Currency.USD, (FlatPerClip(amount=Money.usd("0.10")),)
            ),
            ProviderPricing(
                "pricey", "m", Currency.USD, (FlatPerClip(amount=Money.usd("0.50")),)
            ),
        ]
    )


async def test_cheapest_capable_picks_lowest_expected() -> None:
    reg = _two_provider_registry()
    est = CostEstimator(reg)
    enf = _enforcer(BudgetCaps.usd(Money.usd("30.00")))
    cands = [CapabilityCandidate("cheap", "m"), CapabilityCandidate("pricey", "m")]
    choice = await cheapest_capable(_req(), cands, estimator=est, enforcer=enf)
    assert choice is not None
    assert choice.provider == "cheap"
    assert choice.estimate.expected == Money.usd("0.10")


async def test_cheapest_capable_skips_unaffordable_high_estimate() -> None:
    # Low-confidence cheap provider whose HIGH estimate breaches the tiny cap;
    # the pricey-but-confident one fits. We commit on worst-case, so pick pricey.
    reg = PricingRegistry(
        [
            ProviderPricing(
                "cheap_risky", "m", Currency.USD,
                (FlatPerClip(amount=Money.usd("0.10")),), confidence=0.0,  # wide band
            ),
            ProviderPricing(
                "safe", "m", Currency.USD,
                (FlatPerClip(amount=Money.usd("0.12")),), confidence=1.0,  # tight
            ),
        ]
    )
    est = CostEstimator(reg, band_fraction=Decimal("0.50"))
    # cheap_risky high = 0.10 * 1.5 = 0.15 > cap; safe high = 0.12 <= cap.
    enf = _enforcer(BudgetCaps.usd(Money.usd("0.12")))
    cands = [CapabilityCandidate("cheap_risky", "m"), CapabilityCandidate("safe", "m")]
    choice = await cheapest_capable(_req(), cands, estimator=est, enforcer=enf)
    assert choice is not None
    assert choice.provider == "safe"


async def test_cheapest_capable_none_when_nothing_fits() -> None:
    reg = _two_provider_registry()
    est = CostEstimator(reg)
    enf = _enforcer(BudgetCaps.usd(Money.usd("0.05")))  # below every option
    cands = [CapabilityCandidate("cheap", "m"), CapabilityCandidate("pricey", "m")]
    assert await cheapest_capable(_req(), cands, estimator=est, enforcer=enf) is None


async def test_cheapest_capable_fail_on_empty_raises_typed() -> None:
    reg = _two_provider_registry()
    est = CostEstimator(reg)
    enf = _enforcer(BudgetCaps.usd(Money.usd("0.05")))
    cands = [CapabilityCandidate("cheap", "m")]
    with pytest.raises(BudgetExceeded) as ei:
        await cheapest_capable(
            VideoCostRequest(duration_s=6), cands, estimator=est, enforcer=enf, fail_on_empty=True
        )
    assert ei.value.scope == "global"


async def test_cheapest_capable_free_tier_changes_winner() -> None:
    # With Wan free seconds intact it is cheapest (0.00); once exhausted MiniMax wins.
    from app.video.cost.pricing import default_registry

    est = CostEstimator(default_registry())
    enf = _enforcer(BudgetCaps.usd(Money.usd("30.00")))
    cands = [
        CapabilityCandidate("minimax", "MiniMax-Hailuo-2.3-Fast"),
        CapabilityCandidate("dashscope", "wan2.1-t2v-turbo"),
    ]
    req = VideoCostRequest(duration_s=6, resolution="720P")

    fresh = await cheapest_capable(req, cands, estimator=est, enforcer=enf)
    assert fresh is not None and fresh.provider == "dashscope"

    exhausted = await cheapest_capable(
        req, cands, estimator=est, enforcer=enf, quota=StaticQuotaView({"dashscope": 1650})
    )
    assert exhausted is not None and exhausted.provider == "minimax"


async def test_cheapest_capable_skips_unregistered_candidate() -> None:
    reg = _two_provider_registry()
    est = CostEstimator(reg)
    enf = _enforcer(BudgetCaps.usd(Money.usd("30.00")))
    cands = [CapabilityCandidate("ghost", "m"), CapabilityCandidate("cheap", "m")]
    choice = await cheapest_capable(_req(), cands, estimator=est, enforcer=enf)
    assert choice is not None and choice.provider == "cheap"
