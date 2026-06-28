"""Multi-cloud provider abstraction + capability negotiation.

Kinora already speaks to more than one cloud: DashScope (chat/VL/image/TTS/Wan)
and — for the reasoning brain — OpenAI (CLAUDE.md: ``REASONING_PROVIDER``). The
§12.6 Alibaba ``VideoSynthesis`` worker is a third lane, and a self-hosted fallback
is plausible. Each cloud supports a *different subset* of capabilities (OpenAI does
chat but not Wan video; the Alibaba worker does video but not chat).

This module is the registry the gateway routes through. It records, per registered
provider, **which capabilities it offers** and **which model ids back each**, then
*negotiates*: given a requested capability (+ optional model preference), it returns
the providers that can serve it, in priority order. No provider is hard-coded — the
registry is populated by descriptors, so a fourth cloud is one ``register`` call.

Pure logic, no I/O, no settings reads — the gateway constructs descriptors from
settings and feeds them in. Exhaustively testable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class Capability(StrEnum):
    """A provider capability the gateway can negotiate for."""

    CHAT = "chat"
    VISION = "vision"  # vision-language (the Critic)
    IMAGE = "image"  # image generation
    IMAGE_EDIT = "image_edit"
    TTS = "tts"
    ASR = "asr"  # forced-alignment / transcription
    EMBED = "embed"
    VIDEO_T2V = "video_t2v"
    VIDEO_I2V = "video_i2v"
    VIDEO_R2V = "video_r2v"


class Cloud(StrEnum):
    """The cloud / vendor a provider belongs to."""

    DASHSCOPE = "dashscope"
    OPENAI = "openai"
    ALIBABA = "alibaba"  # the §12.6 VideoSynthesis worker lane
    SELFHOST = "selfhost"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    """A registered provider's identity, capabilities, and per-capability models.

    Attributes:
        name: Stable unique id (routing, telemetry).
        cloud: Which cloud/vendor it belongs to.
        capabilities: The set of capabilities it can serve.
        models: capability -> the model ids that back it (first = default).
        priority: Lower = preferred. Ties break on registration order.
        cost_weight: Relative spend weight (the gateway can prefer cheaper clouds
            when a budget floor is near; pure ratio, units irrelevant).
        quality: 0..1 fidelity score for quality-first ordering.
    """

    name: str
    cloud: Cloud
    capabilities: frozenset[Capability]
    models: dict[Capability, tuple[str, ...]] = field(default_factory=dict)
    priority: int = 100
    cost_weight: float = 1.0
    quality: float = 0.5

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def models_for(self, capability: Capability) -> tuple[str, ...]:
        return self.models.get(capability, ())

    def serves_model(self, capability: Capability, model: str) -> bool:
        return model in self.models_for(capability)


@dataclass(frozen=True, slots=True)
class NegotiationResult:
    """The outcome of a :meth:`ProviderRegistry.negotiate` call."""

    capability: Capability
    providers: tuple[ProviderDescriptor, ...]
    #: The model id chosen for the *preferred* provider (resolved from the
    #: request preference or the provider's default for the capability).
    chosen_model: str | None

    @property
    def satisfied(self) -> bool:
        return bool(self.providers)

    @property
    def preferred(self) -> ProviderDescriptor | None:
        return self.providers[0] if self.providers else None


class CapabilityUnavailable(RuntimeError):  # noqa: N818 - public name in contract
    """No registered provider can serve the requested capability (+ model)."""


class ProviderRegistry:
    """A registry of :class:`ProviderDescriptor` s with capability negotiation.

    Build empty, then ``register`` descriptors (the gateway constructs them from
    settings). ``negotiate`` returns the providers that can serve a capability in
    the requested order; ``require`` is the raising variant for must-succeed paths.
    """

    def __init__(self, descriptors: Iterable[ProviderDescriptor] | None = None) -> None:
        self._by_name: dict[str, ProviderDescriptor] = {}
        self._order: list[str] = []
        for desc in descriptors or ():
            self.register(desc)

    # -- registration ----------------------------------------------------- #

    def register(self, descriptor: ProviderDescriptor) -> None:
        """Add/replace a provider descriptor (idempotent by name)."""
        if descriptor.name not in self._by_name:
            self._order.append(descriptor.name)
        self._by_name[descriptor.name] = descriptor

    def deregister(self, name: str) -> bool:
        if name in self._by_name:
            del self._by_name[name]
            self._order.remove(name)
            return True
        return False

    def get(self, name: str) -> ProviderDescriptor | None:
        return self._by_name.get(name)

    def all(self) -> list[ProviderDescriptor]:
        return [self._by_name[n] for n in self._order]

    def capabilities(self) -> set[Capability]:
        """The union of every registered provider's capabilities."""
        caps: set[Capability] = set()
        for desc in self._by_name.values():
            caps |= desc.capabilities
        return caps

    # -- negotiation ------------------------------------------------------ #

    def providers_for(self, capability: Capability) -> list[ProviderDescriptor]:
        """All providers that support ``capability``, in priority order (pure)."""
        candidates = [d for d in self._by_name.values() if d.supports(capability)]
        # Stable sort: (priority asc, registration index asc).
        index = {name: i for i, name in enumerate(self._order)}
        candidates.sort(key=lambda d: (d.priority, index[d.name]))
        return candidates

    def negotiate(
        self,
        capability: Capability,
        *,
        prefer_model: str | None = None,
        prefer_cloud: Cloud | None = None,
        budget_low: bool = False,
    ) -> NegotiationResult:
        """Pick providers for ``capability``, honoring preferences (pure logic).

        Ordering rules, applied in this precedence:

        1. A provider that *serves the exact* ``prefer_model`` floats to the front.
        2. A provider in ``prefer_cloud`` is preferred among the rest.
        3. ``budget_low`` → ascending ``cost_weight`` (cheapest first); otherwise
           descending ``quality`` (best first).
        4. Finally the registry's priority order breaks remaining ties (stable).

        ``chosen_model`` resolves to ``prefer_model`` when the front provider serves
        it, else that provider's default model for the capability.
        """
        candidates = self.providers_for(capability)
        if not candidates:
            return NegotiationResult(capability=capability, providers=(), chosen_model=None)

        index = {name: i for i, name in enumerate(self._order)}

        def key(desc: ProviderDescriptor) -> tuple[int, int, float, int]:
            has_model = bool(prefer_model and desc.serves_model(capability, prefer_model))
            serves_model = 0 if has_model else 1
            cloud_match = 0 if (prefer_cloud and desc.cloud is prefer_cloud) else 1
            tier = desc.cost_weight if budget_low else -desc.quality
            return (serves_model, cloud_match, tier, index[desc.name])

        ordered = sorted(candidates, key=key)
        front = ordered[0]
        chosen: str | None
        if prefer_model and front.serves_model(capability, prefer_model):
            chosen = prefer_model
        else:
            models = front.models_for(capability)
            chosen = models[0] if models else None
        return NegotiationResult(
            capability=capability,
            providers=tuple(ordered),
            chosen_model=chosen,
        )

    def require(
        self,
        capability: Capability,
        *,
        prefer_model: str | None = None,
        prefer_cloud: Cloud | None = None,
        budget_low: bool = False,
    ) -> NegotiationResult:
        """Like :meth:`negotiate` but raises :class:`CapabilityUnavailable` on miss."""
        result = self.negotiate(
            capability,
            prefer_model=prefer_model,
            prefer_cloud=prefer_cloud,
            budget_low=budget_low,
        )
        if not result.satisfied:
            raise CapabilityUnavailable(
                f"no registered provider serves capability {capability.value!r}"
                + (f" with model {prefer_model!r}" if prefer_model else "")
            )
        return result


# --------------------------------------------------------------------------- #
# Descriptor factories — build the standard Kinora clouds from id lists
# --------------------------------------------------------------------------- #


def dashscope_descriptor(
    *,
    chat_models: Sequence[str],
    vl_models: Sequence[str],
    image_models: Sequence[str],
    image_edit_models: Sequence[str],
    tts_models: Sequence[str],
    asr_models: Sequence[str] = (),
    embed_models: Sequence[str],
    t2v_models: Sequence[str],
    i2v_models: Sequence[str],
    r2v_models: Sequence[str],
    priority: int = 10,
) -> ProviderDescriptor:
    """The full DashScope descriptor (the one cloud that serves nearly everything)."""
    models: dict[Capability, tuple[str, ...]] = {}
    caps: set[Capability] = set()

    def put(cap: Capability, ids: Sequence[str]) -> None:
        if ids:
            models[cap] = tuple(ids)
            caps.add(cap)

    put(Capability.CHAT, chat_models)
    put(Capability.VISION, vl_models)
    put(Capability.IMAGE, image_models)
    put(Capability.IMAGE_EDIT, image_edit_models)
    put(Capability.TTS, tts_models)
    put(Capability.ASR, asr_models)
    put(Capability.EMBED, embed_models)
    put(Capability.VIDEO_T2V, t2v_models)
    put(Capability.VIDEO_I2V, i2v_models)
    put(Capability.VIDEO_R2V, r2v_models)
    return ProviderDescriptor(
        name="dashscope",
        cloud=Cloud.DASHSCOPE,
        capabilities=frozenset(caps),
        models=models,
        priority=priority,
        cost_weight=1.0,
        quality=0.7,
    )


def openai_descriptor(*, chat_models: Sequence[str], priority: int = 5) -> ProviderDescriptor:
    """The OpenAI reasoning descriptor (chat only; the GPT-5 brain)."""
    return ProviderDescriptor(
        name="openai",
        cloud=Cloud.OPENAI,
        capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: tuple(chat_models)},
        priority=priority,
        cost_weight=2.0,  # the reasoning brain is the premium lane
        quality=0.9,
    )


__all__ = [
    "Capability",
    "CapabilityUnavailable",
    "Cloud",
    "NegotiationResult",
    "ProviderDescriptor",
    "ProviderRegistry",
    "dashscope_descriptor",
    "openai_descriptor",
]
