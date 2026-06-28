"""Model registry + capability / cost routing config.

The crew hard-binds model ids at construction; ``app.optim.routing`` lets an
operator override a *call-site* → model. This module is the **catalog** that makes
those decisions informed: it records each model's *capabilities* (chat, vision,
function-calling, JSON-mode), *context window*, *modality*, and *cost* (per-1k
tokens), then answers the routing question structurally — "give me the cheapest
registered model that can do vision + function-calling with a ≥ 32k window."

It deliberately mirrors the spend dimensions of ``app.optim.cost_meter`` (per-1k
in/out tokens) so a routing decision here and a cost reading there agree, but it
stays independent (no import of the optim layer) so it is unit-testable in
isolation and additive to the existing routing seam rather than replacing it.

Pure module — no model calls, no app imports beyond the package error types.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from app.llmops.errors import ModelNotRegisteredError, NoCapableModelError


class Capability(StrEnum):
    CHAT = "chat"
    VISION = "vision"
    FUNCTION_CALLING = "function_calling"
    JSON_MODE = "json_mode"
    STREAMING = "streaming"
    LONG_CONTEXT = "long_context"  # >= 100k tokens


class Modality(StrEnum):
    TEXT = "text"
    MULTIMODAL = "multimodal"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    EMBEDDING = "embedding"


@dataclass(frozen=True, slots=True)
class ModelCard:
    """A registered model and what it can do / costs."""

    id: str
    provider: str  # "dashscope" | "openai" | "wan" | ...
    modality: Modality
    capabilities: frozenset[Capability]
    context_window: int  # tokens
    input_per_1k: Decimal = Decimal("0")
    output_per_1k: Decimal = Decimal("0")
    #: A coarse 0..1 quality tier (operator-supplied; used only as a tie-breaker).
    quality: float = 0.5
    notes: str = ""

    def has(self, *capabilities: Capability) -> bool:
        return all(c in self.capabilities for c in capabilities)

    def cost_per_1k_combined(self, *, in_out_ratio: float = 3.0) -> Decimal:
        """A single comparable price: weighted by a typical input:output token ratio.

        Most agent calls are input-heavy (a big canon slice in, a small JSON out),
        so the default ratio weights input 3:1. Pure scalar for ranking.
        """
        ratio = Decimal(str(in_out_ratio))
        denom = ratio + Decimal("1")
        return (self.input_per_1k * ratio + self.output_per_1k) / denom


@dataclass(frozen=True, slots=True)
class RoutingRequest:
    """A capability/cost query against the registry."""

    required: frozenset[Capability] = field(default_factory=frozenset)
    min_context: int = 0
    modality: Modality | None = None
    provider: str | None = None
    #: Rank by cost (default) or quality.
    objective: str = "cost"  # "cost" | "quality"
    #: A hard ceiling on the combined per-1k price (None = no ceiling).
    max_cost_per_1k: Decimal | None = None


@dataclass
class ModelRegistry:
    """Catalog of :class:`ModelCard`s + capability/cost routing."""

    _cards: dict[str, ModelCard] = field(default_factory=dict)

    # -- registration -------------------------------------------------------- #

    def register(self, card: ModelCard) -> None:
        self._cards[card.id] = card

    def register_all(self, cards: list[ModelCard]) -> None:
        for card in cards:
            self.register(card)

    # -- reads --------------------------------------------------------------- #

    def get(self, model_id: str) -> ModelCard:
        try:
            return self._cards[model_id]
        except KeyError as exc:
            raise ModelNotRegisteredError(model_id) from exc

    def has(self, model_id: str) -> bool:
        return model_id in self._cards

    def all(self) -> list[ModelCard]:
        return sorted(self._cards.values(), key=lambda c: c.id)

    def ids(self) -> list[str]:
        return sorted(self._cards)

    # -- routing ------------------------------------------------------------- #

    def candidates(self, request: RoutingRequest) -> list[ModelCard]:
        """Every card satisfying the request's hard constraints (unranked)."""
        out: list[ModelCard] = []
        for card in self._cards.values():
            if request.modality is not None and card.modality is not request.modality:
                continue
            if request.provider is not None and card.provider != request.provider:
                continue
            if card.context_window < request.min_context:
                continue
            if not card.has(*request.required):
                continue
            if (
                request.max_cost_per_1k is not None
                and card.cost_per_1k_combined() > request.max_cost_per_1k
            ):
                continue
            out.append(card)
        return out

    def route(self, request: RoutingRequest) -> ModelCard:
        """Pick the best card for the request (raises :class:`NoCapableModelError`)."""
        cands = self.candidates(request)
        if not cands:
            raise NoCapableModelError(
                f"no registered model satisfies {sorted(c.value for c in request.required)} "
                f"@ >= {request.min_context} ctx"
            )
        if request.objective == "quality":
            # Highest quality; cheaper breaks a tie.
            cands.sort(key=lambda c: (-c.quality, c.cost_per_1k_combined()))
        else:
            # Cheapest; higher quality breaks a tie.
            cands.sort(key=lambda c: (c.cost_per_1k_combined(), -c.quality))
        return cands[0]

    def cheapest_for(self, *required: Capability, min_context: int = 0) -> ModelCard:
        """Convenience: cheapest model with the given capabilities."""
        return self.route(
            RoutingRequest(required=frozenset(required), min_context=min_context, objective="cost")
        )


# --------------------------------------------------------------------------- #
# Default catalog — the ids Kinora ships against (illustrative prices)
# --------------------------------------------------------------------------- #
#
# Prices are illustrative list prices (the ids are past the training cutoff and
# vary by region — same caveat as app.optim.cost_meter). The *relative* ordering
# is what routing relies on; absolute calibration is operator-tunable.

_CHAT = frozenset({Capability.CHAT, Capability.JSON_MODE, Capability.STREAMING})
_CHAT_TOOLS = _CHAT | {Capability.FUNCTION_CALLING}


def default_catalog() -> ModelRegistry:
    """A registry pre-loaded with Kinora's model stack (§11)."""
    registry = ModelRegistry()
    registry.register_all(
        [
            ModelCard(
                id="qwen3.7-max",
                provider="dashscope",
                modality=Modality.TEXT,
                capabilities=_CHAT_TOOLS | {Capability.LONG_CONTEXT},
                context_window=131072,
                input_per_1k=Decimal("0.0024"),
                output_per_1k=Decimal("0.0096"),
                quality=0.95,
                notes="Showrunner tier.",
            ),
            ModelCard(
                id="qwen3.7-plus",
                provider="dashscope",
                modality=Modality.TEXT,
                capabilities=_CHAT_TOOLS | {Capability.LONG_CONTEXT},
                context_window=131072,
                input_per_1k=Decimal("0.0008"),
                output_per_1k=Decimal("0.0020"),
                quality=0.85,
                notes="Adapter / Continuity / Cinematographer tier.",
            ),
            ModelCard(
                id="qwen3.5-plus",
                provider="dashscope",
                modality=Modality.TEXT,
                capabilities=_CHAT_TOOLS,
                context_window=65536,
                input_per_1k=Decimal("0.0005"),
                output_per_1k=Decimal("0.0015"),
                quality=0.75,
                notes="Cheapest chat tier (comment classifier candidate).",
            ),
            ModelCard(
                id="qwen-vl-max",
                provider="dashscope",
                modality=Modality.MULTIMODAL,
                capabilities=_CHAT | {Capability.VISION},
                context_window=32768,
                input_per_1k=Decimal("0.0012"),
                output_per_1k=Decimal("0.0036"),
                quality=0.88,
                notes="Critic (looks at clip frames).",
            ),
            ModelCard(
                id="gpt-5.5",
                provider="openai",
                modality=Modality.TEXT,
                capabilities=_CHAT_TOOLS | {Capability.LONG_CONTEXT},
                context_window=200000,
                input_per_1k=Decimal("0.0050"),
                output_per_1k=Decimal("0.0150"),
                quality=0.97,
                notes="Optional reasoning provider (REASONING_PROVIDER=openai).",
            ),
            ModelCard(
                id="tongyi-embedding-vision-plus",
                provider="dashscope",
                modality=Modality.EMBEDDING,
                capabilities=frozenset(),
                context_window=8192,
                input_per_1k=Decimal("0.0001"),
                quality=0.8,
                notes="1152-dim multimodal embeddings (CCS / retrieval).",
            ),
        ]
    )
    return registry


__all__ = [
    "Capability",
    "ModelCard",
    "ModelRegistry",
    "Modality",
    "RoutingRequest",
    "default_catalog",
]
