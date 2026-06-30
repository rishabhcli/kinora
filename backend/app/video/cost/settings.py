"""Build the cost layer from application :class:`~app.core.config.Settings`.

The declarative pricing model is the contract; this module is the one place that
reads Kinora's concrete config (the MiniMax flat per-clip price, the Wan free
tier, the $30 USD ceiling) and turns it into a registry / caps / wired layer. It
takes :class:`Settings` (or anything with the same attributes) by *duck typing*
so it stays importable without forcing a settings load, and so tests can pass a
tiny stub.

Nothing here reaches the network or mutates global state — it only assembles the
pure objects from :mod:`app.video.cost`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from app.video.cost.enforcement import BudgetCaps, BudgetEnforcer
from app.video.cost.estimator import CostEstimator
from app.video.cost.ledger import InMemorySpendLedger, SpendLedger
from app.video.cost.money import Currency, Money
from app.video.cost.pricing import (
    FlatPerClip,
    MinimumCharge,
    PerResolutionTier,
    PricingRegistry,
    ProviderPricing,
    SurgeMultiplier,
)
from app.video.cost.reconcile import DriftRecorder


class _CostSettings(Protocol):
    """The subset of :class:`Settings` the cost layer reads (structural typing)."""

    minimax_video_model: str
    minimax_cost_per_clip_usd: float
    video_model: str
    budget_ceiling_usd: float
    budget_ceiling_video_s: float


#: Representative Wan paid per-second overflow rate (see pricing.default_registry).
_WAN_OVERFLOW_USD = "0.10"


def registry_from_settings(settings: _CostSettings) -> PricingRegistry:
    """A :class:`PricingRegistry` seeded from concrete Kinora config.

    * MiniMax: a flat per-clip charge equal to ``minimax_cost_per_clip_usd`` (its
      published model), floored by a matching minimum charge.
    * Wan (dashscope): free up to ``budget_ceiling_video_s`` seconds, then a
      representative per-resolution per-second rate. Lower confidence than MiniMax
      because the overflow rate is estimated, not the published number.
    """
    minimax = ProviderPricing(
        provider="minimax",
        model=settings.minimax_video_model,
        currency=Currency.USD,
        free_tier_seconds=None,
        confidence=1.0,
        notes="config minimax_cost_per_clip_usd (published flat per-clip)",
        components=(
            FlatPerClip(amount=Money.from_float(settings.minimax_cost_per_clip_usd, Currency.USD)),
            MinimumCharge(
                minimum=Money.from_float(settings.minimax_cost_per_clip_usd, Currency.USD)
            ),
        ),
    )
    overflow = Money.usd(_WAN_OVERFLOW_USD)
    wan = ProviderPricing(
        provider="dashscope",
        model=settings.video_model,
        currency=Currency.USD,
        free_tier_seconds=int(settings.budget_ceiling_video_s),
        confidence=0.7,
        notes="kinora.md §11.1 free tier; representative overflow per-second rate",
        components=(
            PerResolutionTier(
                rates={
                    "480P": Money.usd("0.05"),
                    "720P": overflow,
                    "768P": overflow,
                    "1080P": Money.usd("0.20"),
                },
                default_rate=overflow,
            ),
            SurgeMultiplier(multiplier=Decimal("1.0")),
        ),
    )
    return PricingRegistry([minimax, wan])


def caps_from_settings(
    settings: _CostSettings,
    *,
    per_provider_usd: dict[str, str] | None = None,
    per_book_usd: str | None = None,
    soft_cap_fraction: Decimal = Decimal("0.90"),
) -> BudgetCaps:
    """:class:`BudgetCaps` with the §11.1 ~$30 global ceiling from config.

    Optional per-provider / per-book caps are passed as decimal strings (exact).
    """
    return BudgetCaps.usd(
        global_cap=Money.from_float(settings.budget_ceiling_usd, Currency.USD),
        per_provider={
            p: Money.usd(v) for p, v in (per_provider_usd or {}).items()
        },
        per_book=Money.usd(per_book_usd) if per_book_usd is not None else None,
        soft_cap_fraction=soft_cap_fraction,
    )


@dataclass(frozen=True, slots=True)
class CostLayer:
    """The fully-wired cost subsystem, ready for the router to consult.

    A bundle, not a service: the router calls
    :func:`~app.video.cost.enforcement.cheapest_capable` with ``layer.estimator``
    and ``layer.enforcer``, reserves through ``layer.enforcer``, and feeds commits
    to ``layer.drift``.
    """

    registry: PricingRegistry
    estimator: CostEstimator
    ledger: SpendLedger
    enforcer: BudgetEnforcer
    drift: DriftRecorder


def cost_layer_from_settings(
    settings: _CostSettings,
    *,
    ledger: SpendLedger | None = None,
    per_provider_usd: dict[str, str] | None = None,
    per_book_usd: str | None = None,
) -> CostLayer:
    """Assemble a :class:`CostLayer` from config (default: in-memory ledger).

    Pass a :class:`~app.video.cost.ledger.RedisSpendLedger` as ``ledger`` for the
    cross-process production path; the default in-memory ledger suits a single
    process / tests.
    """
    registry = registry_from_settings(settings)
    estimator = CostEstimator(registry)
    spend_ledger: SpendLedger = ledger or InMemorySpendLedger(Currency.USD)
    caps = caps_from_settings(
        settings, per_provider_usd=per_provider_usd, per_book_usd=per_book_usd
    )
    enforcer = BudgetEnforcer(spend_ledger, caps)
    return CostLayer(
        registry=registry,
        estimator=estimator,
        ledger=spend_ledger,
        enforcer=enforcer,
        drift=DriftRecorder(Currency.USD),
    )


__all__ = [
    "CostLayer",
    "caps_from_settings",
    "cost_layer_from_settings",
    "registry_from_settings",
]
