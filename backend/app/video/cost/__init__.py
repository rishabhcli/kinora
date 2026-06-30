"""Cross-provider video **cost & budget normalization** (kinora.md §11.1).

Heterogeneous video models (Wan turbo, MiniMax Hailuo, a future HappyHorse t2v)
quote prices in incompatible shapes — flat per-clip, per-second, per-resolution
tier, per-frame, with surge windows, free-tier quotas, and minimum charges. This
layer puts them all on **one ruler** so the video router can answer a single
question deterministically: *for this exact render, which capable provider is
cheapest, and can we afford it under every cap?*

The pieces, all pure logic with injectable fakes (no infra, no clock, never any
spend):

* :mod:`~app.video.cost.money` — an exact integer-minor-unit :class:`Money` type
  (no float drift) with explicit multi-currency FX.
* :mod:`~app.video.cost.request` — the provider-agnostic :class:`VideoCostRequest`
  (and a :func:`~app.video.cost.request.from_wan_spec` adapter).
* :mod:`~app.video.cost.pricing` — declarative :class:`PriceComponent` rules +
  a per-provider :class:`ProviderPricing` sheet and a :class:`PricingRegistry`.
* :mod:`~app.video.cost.estimator` — a :class:`CostEstimator` returning a
  predicted cost with a confidence-scaled uncertainty band.
* :mod:`~app.video.cost.ledger` — a two-phase :class:`SpendLedger`
  (reserve→commit→release), in-memory + a Redis-backed interface mirroring the
  existing :class:`~app.providers.minimax.RedisSpendStore` semantics.
* :mod:`~app.video.cost.enforcement` — layered money caps (global + per-provider
  + per-book) raising a typed :class:`BudgetExceeded`, and the
  :func:`cheapest_capable` helper the router calls.
* :mod:`~app.video.cost.reconcile` — estimated-vs-actual :class:`DriftRecorder`.

It is **additive** and **off the critical path**: nothing here changes the
existing :class:`~app.providers.minimax.MiniMaxVideoProvider` USD guard or the
scheduler's video-seconds :class:`~app.memory.budget_service.BudgetService`; it
sits beside them so a future router wiring is opt-in.
"""

from __future__ import annotations

from app.video.cost.enforcement import (
    AffordabilityCheck,
    BudgetCaps,
    BudgetEnforcer,
    BudgetExceeded,
    CapabilityCandidate,
    ProviderChoice,
    cheapest_capable,
)
from app.video.cost.estimator import (
    ZERO_QUOTA,
    CostEstimate,
    CostEstimator,
    QuotaView,
    StaticQuotaView,
)
from app.video.cost.ledger import (
    InMemorySpendLedger,
    LedgerError,
    ProviderSpend,
    RedisLedgerTransport,
    RedisSpendLedger,
    Reservation,
    ReservationState,
    SpendLedger,
    SpendScope,
)
from app.video.cost.money import (
    MINOR_UNIT_SCALE,
    Currency,
    CurrencyMismatch,
    FxConverter,
    Money,
)
from app.video.cost.pricing import (
    FlatPerClip,
    MinimumCharge,
    PerFrame,
    PerResolutionTier,
    PerSecond,
    PriceComponent,
    PricingContext,
    PricingRegistry,
    PriorityMultiplier,
    ProviderPricing,
    SurgeMultiplier,
    default_registry,
)
from app.video.cost.reconcile import DriftRecorder, DriftSample, ProviderDrift
from app.video.cost.request import VideoCostRequest, VideoMode, from_wan_spec
from app.video.cost.settings import (
    CostLayer,
    caps_from_settings,
    cost_layer_from_settings,
    registry_from_settings,
)

__all__ = [
    "MINOR_UNIT_SCALE",
    "ZERO_QUOTA",
    "AffordabilityCheck",
    "BudgetCaps",
    "BudgetEnforcer",
    "BudgetExceeded",
    "CapabilityCandidate",
    "CostEstimate",
    "CostEstimator",
    "CostLayer",
    "Currency",
    "CurrencyMismatch",
    "DriftRecorder",
    "DriftSample",
    "FlatPerClip",
    "FxConverter",
    "InMemorySpendLedger",
    "LedgerError",
    "MinimumCharge",
    "Money",
    "PerFrame",
    "PerResolutionTier",
    "PerSecond",
    "PriceComponent",
    "PricingContext",
    "PricingRegistry",
    "PriorityMultiplier",
    "ProviderChoice",
    "ProviderDrift",
    "ProviderPricing",
    "ProviderSpend",
    "QuotaView",
    "RedisLedgerTransport",
    "RedisSpendLedger",
    "Reservation",
    "ReservationState",
    "SpendLedger",
    "SpendScope",
    "StaticQuotaView",
    "SurgeMultiplier",
    "VideoCostRequest",
    "VideoMode",
    "caps_from_settings",
    "cheapest_capable",
    "cost_layer_from_settings",
    "default_registry",
    "from_wan_spec",
    "registry_from_settings",
]
