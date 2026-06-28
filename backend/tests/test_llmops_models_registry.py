"""Unit tests for the model registry + capability/cost routing (no infra)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.llmops.errors import ModelNotRegisteredError, NoCapableModelError
from app.llmops.models_registry import (
    Capability,
    Modality,
    ModelCard,
    ModelRegistry,
    RoutingRequest,
    default_catalog,
)


def _card(
    id_: str, price_in: str, *caps: Capability, ctx: int = 32768, quality: float = 0.5
) -> ModelCard:
    return ModelCard(
        id=id_,
        provider="test",
        modality=Modality.TEXT,
        capabilities=frozenset(caps),
        context_window=ctx,
        input_per_1k=Decimal(price_in),
        output_per_1k=Decimal(price_in),
        quality=quality,
    )


def test_default_catalog_has_crew_models() -> None:
    reg = default_catalog()
    for model_id in ("qwen3.7-max", "qwen3.7-plus", "qwen-vl-max", "gpt-5.5"):
        assert reg.has(model_id)


def test_get_unknown_raises() -> None:
    with pytest.raises(ModelNotRegisteredError):
        default_catalog().get("nope")


def test_route_cheapest() -> None:
    reg = ModelRegistry()
    reg.register(_card("cheap", "0.001", Capability.CHAT))
    reg.register(_card("pricey", "0.010", Capability.CHAT))
    chosen = reg.route(RoutingRequest(required=frozenset({Capability.CHAT}), objective="cost"))
    assert chosen.id == "cheap"


def test_route_quality_objective() -> None:
    reg = ModelRegistry()
    reg.register(_card("cheap_lo", "0.001", Capability.CHAT, quality=0.4))
    reg.register(_card("pricey_hi", "0.010", Capability.CHAT, quality=0.9))
    chosen = reg.route(RoutingRequest(required=frozenset({Capability.CHAT}), objective="quality"))
    assert chosen.id == "pricey_hi"


def test_capability_filter() -> None:
    reg = default_catalog()
    vision = reg.route(RoutingRequest(required=frozenset({Capability.VISION})))
    assert Capability.VISION in vision.capabilities
    assert vision.id == "qwen-vl-max"


def test_min_context_filter() -> None:
    reg = default_catalog()
    big = reg.route(RoutingRequest(required=frozenset({Capability.CHAT}), min_context=180000))
    assert big.context_window >= 180000


def test_no_capable_model_raises() -> None:
    reg = ModelRegistry()
    reg.register(_card("text_only", "0.001", Capability.CHAT))
    with pytest.raises(NoCapableModelError):
        reg.route(RoutingRequest(required=frozenset({Capability.VISION})))


def test_cost_ceiling() -> None:
    reg = ModelRegistry()
    reg.register(_card("cheap", "0.001", Capability.CHAT))
    reg.register(_card("pricey", "0.500", Capability.CHAT))
    cands = reg.candidates(
        RoutingRequest(required=frozenset({Capability.CHAT}), max_cost_per_1k=Decimal("0.01"))
    )
    assert [c.id for c in cands] == ["cheap"]


def test_cheapest_for_convenience() -> None:
    reg = default_catalog()
    card = reg.cheapest_for(Capability.FUNCTION_CALLING)
    assert Capability.FUNCTION_CALLING in card.capabilities


def test_combined_cost_weighting() -> None:
    # input-heavy weighting (3:1) means input price dominates.
    card = ModelCard(
        id="m",
        provider="t",
        modality=Modality.TEXT,
        capabilities=frozenset({Capability.CHAT}),
        context_window=1000,
        input_per_1k=Decimal("0.004"),
        output_per_1k=Decimal("0.000"),
    )
    # (0.004*3 + 0)/4 = 0.003
    assert card.cost_per_1k_combined() == Decimal("0.003")
