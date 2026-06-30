"""Per-model cost/latency awareness + probability→model routing (§4.6/§11).

Two pieces:

* :class:`TieredCostModel` — a concrete, deterministic :class:`CostModelProtocol`
  built from a table of :class:`ModelSpec` (one per provider video id). It is the
  injectable economics oracle the planner reasons against in tests; production
  adapts the real provider tiers (``app.providers.video_router.BackendTier``) onto
  the same protocol without this module importing it.
* :func:`route_model_for_probability` — the policy that picks *which* model class a
  speculation should use given its hit-probability: **cheap turbo ids for
  low-probability guesses, premium ids reserved for high-probability / committed
  shots** (kinora.md §4.6 — never burn premium seconds on a long-shot guess).

Everything is pure: no env reads, no network.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.video.speculate.types import ModelClass

#: Probability at/above which a speculation earns a *premium* model (it is nearly
#: committed). Below the *standard* threshold it gets the cheapest model.
DEFAULT_PREMIUM_PROBABILITY = 0.7
DEFAULT_STANDARD_PROBABILITY = 0.35


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """The economics of one provider video model id (§11 video-seconds → dollars).

    Attributes:
        model_id: the provider id, e.g. ``wan2.1-i2v-turbo``.
        model_class: its coarse cost/quality tier.
        usd_per_second: dollar cost of one rendered video-second.
        latency_per_second_s: wall seconds to render one video-second (turbo ids
            are faster as well as cheaper).
        fixed_latency_s: per-call submit/poll overhead independent of duration.
        quality: 0..1 fidelity score (premium > standard > cheap).
    """

    model_id: str
    model_class: ModelClass
    usd_per_second: float
    latency_per_second_s: float
    fixed_latency_s: float = 0.0
    quality: float = 0.5


class TieredCostModel:
    """A :class:`CostModelProtocol` over a table of :class:`ModelSpec` (pure)."""

    def __init__(self, specs: Mapping[str, ModelSpec]) -> None:
        if not specs:
            raise ValueError("TieredCostModel requires at least one ModelSpec")
        self._specs = dict(specs)

    @classmethod
    def default(cls) -> TieredCostModel:
        """A representative cheap/standard/premium table (relative units, no env).

        The ratios mirror the real Wan turbo↔quality split documented in
        CLAUDE.md (turbo ids cheaper + faster; quality ids pricier + slower). Only
        ratios matter to the planner, so absolute dollars are illustrative.
        """
        specs = {
            "turbo": ModelSpec(
                model_id="turbo",
                model_class=ModelClass.CHEAP,
                usd_per_second=0.05,
                latency_per_second_s=2.0,
                fixed_latency_s=4.0,
                quality=0.55,
            ),
            "standard": ModelSpec(
                model_id="standard",
                model_class=ModelClass.STANDARD,
                usd_per_second=0.12,
                latency_per_second_s=4.0,
                fixed_latency_s=6.0,
                quality=0.75,
            ),
            "premium": ModelSpec(
                model_id="premium",
                model_class=ModelClass.PREMIUM,
                usd_per_second=0.30,
                latency_per_second_s=8.0,
                fixed_latency_s=10.0,
                quality=0.95,
            ),
        }
        return cls(specs)

    # -- CostModelProtocol ------------------------------------------------- #

    def cost_usd(self, model_id: str, video_seconds: float) -> float:
        spec = self._spec(model_id)
        return round(spec.usd_per_second * max(0.0, video_seconds), 6)

    def latency_s(self, model_id: str, video_seconds: float) -> float:
        spec = self._spec(model_id)
        return round(
            spec.fixed_latency_s + spec.latency_per_second_s * max(0.0, video_seconds),
            6,
        )

    def quality(self, model_id: str) -> float:
        return self._spec(model_id).quality

    def models(self) -> list[str]:
        return list(self._specs)

    # -- routing helpers --------------------------------------------------- #

    def spec(self, model_id: str) -> ModelSpec:
        """The :class:`ModelSpec` for ``model_id``."""
        return self._spec(model_id)

    def class_of(self, model_id: str) -> ModelClass:
        return self._spec(model_id).model_class

    def cheapest_of_class(self, model_class: ModelClass) -> str | None:
        """The lowest-``usd_per_second`` model id in ``model_class`` (or ``None``)."""
        candidates = [s for s in self._specs.values() if s.model_class is model_class]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.usd_per_second).model_id

    def _spec(self, model_id: str) -> ModelSpec:
        try:
            return self._specs[model_id]
        except KeyError as exc:  # pragma: no cover - guard
            raise KeyError(f"unknown model id {model_id!r}") from exc


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """Thresholds for probability→model-class routing (§4.6)."""

    premium_probability: float = DEFAULT_PREMIUM_PROBABILITY
    standard_probability: float = DEFAULT_STANDARD_PROBABILITY


def class_for_probability(
    hit_probability: float,
    policy: RoutingPolicy | None = None,
) -> ModelClass:
    """Map a hit-probability to the model class it deserves (pure policy, §4.6).

    High-probability (nearly committed) → premium; mid → standard; low → cheap.
    This is the rule that *reserves premium models for committed/high-probability
    shots and prefers cheaper models for low-probability speculation.*
    """
    p = policy or RoutingPolicy()
    if hit_probability >= p.premium_probability:
        return ModelClass.PREMIUM
    if hit_probability >= p.standard_probability:
        return ModelClass.STANDARD
    return ModelClass.CHEAP


def route_model_for_probability(
    cost_model: TieredCostModel,
    hit_probability: float,
    *,
    policy: RoutingPolicy | None = None,
) -> str:
    """Pick the concrete model id for a speculation at ``hit_probability``.

    Routes to the deserved class via :func:`class_for_probability`, then to the
    cheapest id *within* that class. Degrades gracefully: if the deserved class is
    empty in the table, falls back down the tiers (premium→standard→cheap) so a
    sparse table still yields a model.
    """
    target = class_for_probability(hit_probability, policy)
    order = {
        ModelClass.PREMIUM: [ModelClass.PREMIUM, ModelClass.STANDARD, ModelClass.CHEAP],
        ModelClass.STANDARD: [ModelClass.STANDARD, ModelClass.CHEAP, ModelClass.PREMIUM],
        ModelClass.CHEAP: [ModelClass.CHEAP, ModelClass.STANDARD, ModelClass.PREMIUM],
    }[target]
    for cls in order:
        model_id = cost_model.cheapest_of_class(cls)
        if model_id is not None:
            return model_id
    # Table is non-empty (constructor guarantees), so this is unreachable, but be
    # total for the type checker.
    return cost_model.models()[0]


__all__ = [
    "DEFAULT_PREMIUM_PROBABILITY",
    "DEFAULT_STANDARD_PROBABILITY",
    "ModelSpec",
    "RoutingPolicy",
    "TieredCostModel",
    "class_for_probability",
    "route_model_for_probability",
]
