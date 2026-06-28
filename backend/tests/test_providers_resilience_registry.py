"""Unit tests for the multi-cloud provider registry + capability negotiation,
and the fan-out metering sink.
"""

from __future__ import annotations

import pytest

from app.providers.resilience.metering import MeteringSink
from app.providers.resilience.registry import (
    Capability,
    CapabilityUnavailable,
    Cloud,
    ProviderDescriptor,
    ProviderRegistry,
    dashscope_descriptor,
    openai_descriptor,
)
from app.providers.types import Usage

# --------------------------------------------------------------------------- #
# Registry + capability negotiation
# --------------------------------------------------------------------------- #


def _ds() -> ProviderDescriptor:
    return dashscope_descriptor(
        chat_models=["qwen3.7-max", "qwen3.7-plus"],
        vl_models=["qwen-vl-max"],
        image_models=["qwen-image-plus"],
        image_edit_models=["qwen-image-edit-max"],
        tts_models=["qwen3-tts-flash"],
        embed_models=["tongyi-embedding-vision-plus"],
        t2v_models=["wan2.1-t2v-turbo"],
        i2v_models=["wan2.1-i2v-turbo"],
        r2v_models=["wan2.1-i2v-turbo"],
    )


def test_dashscope_descriptor_advertises_full_capability_set() -> None:
    ds = _ds()
    assert ds.supports(Capability.CHAT)
    assert ds.supports(Capability.VIDEO_R2V)
    assert ds.models_for(Capability.CHAT) == ("qwen3.7-max", "qwen3.7-plus")
    assert ds.serves_model(Capability.IMAGE, "qwen-image-plus")
    assert not ds.serves_model(Capability.IMAGE, "nonexistent")


def test_registry_negotiate_chat_prefers_openai_by_priority() -> None:
    reg = ProviderRegistry([_ds(), openai_descriptor(chat_models=["gpt-5.5"])])
    result = reg.negotiate(Capability.CHAT)
    assert result.satisfied
    # OpenAI has priority=5 < dashscope priority=10, so it leads for chat.
    assert result.preferred is not None
    assert result.preferred.cloud is Cloud.OPENAI
    assert result.chosen_model == "gpt-5.5"
    # Both providers are returned (failover order).
    assert {d.cloud for d in result.providers} == {Cloud.OPENAI, Cloud.DASHSCOPE}


def test_registry_negotiate_video_only_dashscope() -> None:
    reg = ProviderRegistry([_ds(), openai_descriptor(chat_models=["gpt-5.5"])])
    result = reg.negotiate(Capability.VIDEO_T2V)
    assert result.satisfied
    assert result.preferred is not None
    assert result.preferred.cloud is Cloud.DASHSCOPE
    assert result.chosen_model == "wan2.1-t2v-turbo"


def test_registry_prefer_model_floats_provider_to_front() -> None:
    # Two chat providers; preferring a model only one serves pins that one first.
    p1 = ProviderDescriptor(
        name="a", cloud=Cloud.OTHER, capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: ("model-a",)}, priority=1,
    )
    p2 = ProviderDescriptor(
        name="b", cloud=Cloud.OTHER, capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: ("model-b",)}, priority=2,
    )
    reg = ProviderRegistry([p1, p2])
    result = reg.negotiate(Capability.CHAT, prefer_model="model-b")
    assert result.preferred is not None
    assert result.preferred.name == "b"
    assert result.chosen_model == "model-b"


def test_registry_budget_low_prefers_cheaper_cloud() -> None:
    cheap = ProviderDescriptor(
        name="cheap", cloud=Cloud.OTHER, capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: ("c",)}, priority=10, cost_weight=0.5, quality=0.3,
    )
    premium = ProviderDescriptor(
        name="premium", cloud=Cloud.OTHER, capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: ("p",)}, priority=10, cost_weight=3.0, quality=0.9,
    )
    reg = ProviderRegistry([premium, cheap])
    # Budget healthy -> quality-first (premium).
    healthy = reg.negotiate(Capability.CHAT, budget_low=False)
    assert healthy.preferred is not None and healthy.preferred.name == "premium"
    # Budget low -> cost-first (cheap).
    low = reg.negotiate(Capability.CHAT, budget_low=True)
    assert low.preferred is not None and low.preferred.name == "cheap"


def test_registry_prefer_cloud_breaks_ties() -> None:
    a = ProviderDescriptor(
        name="a", cloud=Cloud.DASHSCOPE, capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: ("x",)}, priority=10, quality=0.5,
    )
    b = ProviderDescriptor(
        name="b", cloud=Cloud.SELFHOST, capabilities=frozenset({Capability.CHAT}),
        models={Capability.CHAT: ("y",)}, priority=10, quality=0.5,
    )
    reg = ProviderRegistry([a, b])
    result = reg.negotiate(Capability.CHAT, prefer_cloud=Cloud.SELFHOST)
    assert result.preferred is not None and result.preferred.cloud is Cloud.SELFHOST


def test_registry_require_raises_when_unavailable() -> None:
    reg = ProviderRegistry([openai_descriptor(chat_models=["gpt-5.5"])])
    # OpenAI has no video capability.
    miss = reg.negotiate(Capability.VIDEO_T2V)
    assert not miss.satisfied
    assert miss.preferred is None
    with pytest.raises(CapabilityUnavailable):
        reg.require(Capability.VIDEO_T2V)


def test_registry_register_deregister_and_union() -> None:
    reg = ProviderRegistry()
    assert reg.capabilities() == set()
    reg.register(_ds())
    assert Capability.CHAT in reg.capabilities()
    reg.register(openai_descriptor(chat_models=["gpt-5.5"]))
    assert len(reg.all()) == 2
    assert reg.deregister("openai") is True
    assert reg.deregister("openai") is False
    assert len(reg.all()) == 1


def test_registry_register_replaces_by_name() -> None:
    reg = ProviderRegistry([_ds()])
    # Re-registering the same name updates in place, keeping a single entry.
    updated = dashscope_descriptor(
        chat_models=["qwen3.7-max"], vl_models=[], image_models=[], image_edit_models=[],
        tts_models=[], embed_models=[], t2v_models=[], i2v_models=[], r2v_models=[],
    )
    reg.register(updated)
    assert len(reg.all()) == 1
    assert reg.get("dashscope") is updated


# --------------------------------------------------------------------------- #
# Metering sink (fan-out + rollups)
# --------------------------------------------------------------------------- #


def test_metering_rolls_up_per_model_and_operation() -> None:
    meter = MeteringSink()
    meter(Usage(model="qwen3.7-max", operation="chat", input_tokens=100, output_tokens=50))
    meter(Usage(model="qwen3.7-max", operation="chat", input_tokens=10, output_tokens=5))
    meter(Usage(model="wan2.1-t2v-turbo", operation="video", video_seconds=5.0))
    snap = meter.snapshot()
    assert snap.total["calls"] == 3
    assert snap.total["input_tokens"] == 110
    assert snap.by_model["qwen3.7-max"]["calls"] == 2
    assert snap.by_operation["video"]["video_seconds"] == 5.0
    assert meter.video_seconds == 5.0
    assert meter.total_tokens == 165


def test_metering_fans_out_to_downstreams() -> None:
    seen_a: list[str] = []
    seen_b: list[str] = []
    meter = MeteringSink([lambda u: seen_a.append(u.model), lambda u: seen_b.append(u.model)])
    assert meter.downstream_count == 2
    meter(Usage(model="m", operation="chat"))
    assert seen_a == ["m"] and seen_b == ["m"]


def test_metering_isolates_a_broken_downstream() -> None:
    seen: list[str] = []

    def broken(_u: Usage) -> None:
        raise RuntimeError("downstream blew up")

    meter = MeteringSink([broken, lambda u: seen.append(u.model)])
    # The broken sink must not propagate, and the healthy sink still fires.
    meter(Usage(model="m", operation="chat"))
    assert seen == ["m"]
    assert meter.snapshot().fanout_errors == 1


def test_metering_add_downstream_late() -> None:
    seen: list[str] = []
    meter = MeteringSink()
    meter(Usage(model="m", operation="chat"))  # before wiring
    meter.add_downstream(lambda u: seen.append(u.model))
    meter(Usage(model="m2", operation="chat"))  # after wiring
    assert seen == ["m2"]  # only the post-wire event reaches the downstream


def test_metering_record_error_and_model_rollup_copy() -> None:
    meter = MeteringSink()
    meter.record_error("qwen-image-plus", "image")
    roll = meter.model_rollup("qwen-image-plus")
    assert roll.errors == 1
    # The returned rollup is a copy: mutating it doesn't affect the meter.
    roll.errors = 999
    assert meter.model_rollup("qwen-image-plus").errors == 1
    assert meter.model_rollup("unknown").calls == 0


def test_metering_reset() -> None:
    meter = MeteringSink()
    meter(Usage(model="m", operation="chat", input_tokens=10))
    meter.reset()
    assert meter.snapshot().total["calls"] == 0
    assert meter.total_tokens == 0
