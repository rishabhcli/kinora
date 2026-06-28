"""Per-agent / per-shot cost attribution (kinora.md §11.1, §12.5). Pure — no infra."""

from __future__ import annotations

from decimal import Decimal

from app.finops.attribution import (
    Agent,
    ShotCostRecorder,
    attribute_agent,
    attribute_by_agent,
    attribute_shot,
)
from app.optim.cost_meter import Price
from app.providers.types import Usage


def test_attribute_agent_by_operation() -> None:
    assert attribute_agent(Usage(model="wan2.7-i2v", operation="video")) is Agent.GENERATOR
    assert attribute_agent(Usage(model="qwen-image-2.0-pro", operation="image")) is Agent.GENERATOR
    assert attribute_agent(Usage(model="qwen3-tts-flash", operation="tts")) is Agent.GENERATOR
    assert attribute_agent(Usage(model="qwen-vl-max", operation="vl")) is Agent.CRITIC
    assert attribute_agent(Usage(model="emb", operation="embedding")) is Agent.CONTINUITY


def test_attribute_chat_disambiguated_by_model() -> None:
    assert attribute_agent(Usage(model="qwen3.7-max", operation="chat")) is Agent.SHOWRUNNER
    assert attribute_agent(Usage(model="qwen3.7-plus", operation="chat")) is Agent.ADAPTER


def test_unknown_operation_is_unknown_agent() -> None:
    assert attribute_agent(Usage(model="x", operation="mystery")) is Agent.UNKNOWN


def test_attribute_by_agent_rolls_up() -> None:
    pricing = {
        "qwen3.7-plus": Price(input_per_1k=Decimal("0.001"), output_per_1k=Decimal("0.002")),
        "qwen-vl-max": Price(input_per_1k=Decimal("0.003")),
    }
    usages = [
        Usage(model="qwen3.7-plus", operation="chat", input_tokens=1000, output_tokens=1000),
        Usage(model="qwen3.7-plus", operation="chat", input_tokens=1000),
        Usage(model="qwen-vl-max", operation="vl", input_tokens=1000),
    ]
    rolled = attribute_by_agent(usages, pricing)
    assert set(rolled) == {Agent.ADAPTER.value, Agent.CRITIC.value}
    assert rolled[Agent.ADAPTER.value]["calls"] == 2
    assert rolled[Agent.CRITIC.value]["calls"] == 1
    # Adapter cost = (0.001+0.002) + (0.001) = 0.004 USD.
    assert rolled[Agent.ADAPTER.value]["cost_usd"] == "0.004"


def test_attribute_shot_uses_charged_video_seconds_override() -> None:
    pricing = {"wan2.7-i2v": Price(per_video_second=Decimal("0.10"))}
    usages = [
        Usage(model="wan2.7-i2v", operation="video", video_seconds=5.0),
        Usage(model="qwen-vl-max", operation="vl", input_tokens=500),
    ]
    # The budget ledger charged 4.8s (rounding), overriding the usage's 5.0.
    cost = attribute_shot("shot_1", usages, charged_video_seconds=4.8, pricing=pricing)
    assert cost.shot_id == "shot_1"
    assert cost.video_seconds == 4.8  # the ledger authority, not the usage 5.0
    assert cost.calls == 2
    # by_agent has both the Generator (video) and Critic (vl).
    assert set(cost.by_agent) == {Agent.GENERATOR.value, Agent.CRITIC.value}


def test_shot_cost_recorder() -> None:
    rec = ShotCostRecorder("shot_x")
    rec.record(Usage(model="qwen3.7-plus", operation="chat", input_tokens=100))
    rec.record(Usage(model="wan2.7-i2v", operation="video", video_seconds=5.0))
    assert len(rec.usages) == 2
    cost = rec.finalize(charged_video_seconds=5.0)
    assert cost.shot_id == "shot_x"
    assert cost.video_seconds == 5.0
    assert cost.calls == 2


def test_attribute_shot_falls_back_to_usage_video_seconds() -> None:
    cost = attribute_shot(
        "s", [Usage(model="wan2.7-i2v", operation="video", video_seconds=3.0)]
    )
    assert cost.video_seconds == 3.0


def test_attributed_rollup_as_dict_serializable() -> None:
    cost = attribute_shot("s", [Usage(model="m", operation="chat", input_tokens=10)])
    d = cost.as_dict()
    assert set(d) >= {"shot_id", "cost_usd", "video_seconds", "by_agent"}
