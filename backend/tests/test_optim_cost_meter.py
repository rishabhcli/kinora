"""Tests for app.optim.cost_meter — USD cost on top of physical Usage units.

The cost meter is the WS1 instrumentation gap: providers track tokens/images/
audio_s/video_s (physical units) but there is no money anywhere. ``cost_of`` is a
pure function (TDD-friendly); ``CostMeter`` is a ``UsageSink`` that rolls cost up
per model / operation / book / session.
"""

from __future__ import annotations

from decimal import Decimal

from app.optim.cost_meter import (
    PRICING,
    CostMeter,
    Price,
    cost_context,
    cost_of,
)
from app.providers.types import Usage


def test_cost_of_chat_prices_input_and_output_tokens_per_1k() -> None:
    pricing = {"m": Price(input_per_1k=Decimal("0.001"), output_per_1k=Decimal("0.002"))}
    usage = Usage(model="m", operation="chat", input_tokens=1000, output_tokens=500)
    # 0.001 * (1000/1000) + 0.002 * (500/1000) = 0.001 + 0.001
    assert cost_of(usage, pricing) == Decimal("0.002")


def test_cost_of_image_prices_per_image() -> None:
    pricing = {"img": Price(per_image=Decimal("0.04"))}
    usage = Usage(model="img", operation="image", images=3)
    assert cost_of(usage, pricing) == Decimal("0.12")


def test_cost_of_video_prices_per_video_second_without_float_drift() -> None:
    pricing = {"wan": Price(per_video_second=Decimal("0.10"))}
    usage = Usage(model="wan", operation="video", video_seconds=5.5)
    # Decimal(str(5.5)) avoids binary-float drift that Decimal(5.5) would introduce.
    assert cost_of(usage, pricing) == Decimal("0.550")


def test_cost_of_unknown_model_is_zero_and_never_raises() -> None:
    usage = Usage(model="not-in-table", operation="chat", input_tokens=10_000)
    assert cost_of(usage, {}) == Decimal("0")


def test_default_pricing_table_prices_a_known_model_above_zero() -> None:
    # The default table is illustrative-but-present: a known chat model must cost > 0.
    usage = Usage(model="qwen3.7-max", operation="chat", input_tokens=1000, output_tokens=1000)
    assert "qwen3.7-max" in PRICING
    assert cost_of(usage) > Decimal("0")


def test_default_pricing_orders_max_more_expensive_than_adapter() -> None:
    # Routing relies on this ordering: max must price strictly above the cheap adapter tier.
    def chat(model: str) -> Usage:
        return Usage(model=model, operation="chat", input_tokens=1000, output_tokens=1000)

    assert cost_of(chat("qwen3.7-max")) > cost_of(chat("qwen3.5-plus"))


def test_meter_accumulates_total_by_model_and_by_operation() -> None:
    pricing = {
        "a": Price(input_per_1k=Decimal("0.001")),
        "b": Price(input_per_1k=Decimal("0.002")),
    }
    meter = CostMeter(pricing=pricing)
    meter(Usage(model="a", operation="chat", input_tokens=1000))
    meter(Usage(model="b", operation="vl", input_tokens=1000))
    snap = meter.snapshot()
    assert snap["total"]["calls"] == 2
    assert Decimal(snap["total"]["cost_usd"]) == Decimal("0.003")
    assert Decimal(snap["by_model"]["a"]["cost_usd"]) == Decimal("0.001")
    assert Decimal(snap["by_operation"]["vl"]["cost_usd"]) == Decimal("0.002")
    assert snap["by_model"]["a"]["input_tokens"] == 1000


def test_cost_context_attributes_cost_to_book_and_session() -> None:
    pricing = {"a": Price(input_per_1k=Decimal("0.001"))}
    meter = CostMeter(pricing=pricing)
    with cost_context(book_id="bk1", session_id="se1"):
        meter(Usage(model="a", operation="chat", input_tokens=2000))
    # Outside the context, attribution is None and only totals move.
    meter(Usage(model="a", operation="chat", input_tokens=1000))
    snap = meter.snapshot()
    assert Decimal(snap["by_book"]["bk1"]["cost_usd"]) == Decimal("0.002")
    assert Decimal(snap["by_session"]["se1"]["cost_usd"]) == Decimal("0.002")
    assert Decimal(snap["total"]["cost_usd"]) == Decimal("0.003")
    assert "bk1" in snap["by_book"] and len(snap["by_book"]) == 1


def test_cost_context_resets_after_exit() -> None:
    pricing = {"a": Price(input_per_1k=Decimal("0.001"))}
    meter = CostMeter(pricing=pricing)
    with cost_context(book_id="bk1"):
        pass
    meter(Usage(model="a", operation="chat", input_tokens=1000))
    snap = meter.snapshot()
    assert snap["by_book"] == {}


def test_meter_reset_clears_all_rollups() -> None:
    meter = CostMeter(pricing={"a": Price(input_per_1k=Decimal("0.001"))})
    meter(Usage(model="a", operation="chat", input_tokens=1000))
    meter.reset()
    snap = meter.snapshot()
    assert snap["total"]["calls"] == 0
    assert snap["by_model"] == {}


def test_meter_never_raises_on_unknown_model() -> None:
    # A hot-path sink must never break a provider call, even with no price entry.
    meter = CostMeter(pricing={})
    meter(Usage(model="mystery", operation="chat", input_tokens=10))
    assert meter.snapshot()["total"]["calls"] == 1


def test_from_settings_reads_pricing_override_json() -> None:
    class _S:
        optim_pricing_json = '{"zzz": {"input_per_1k": "0.5"}}'

    meter = CostMeter.from_settings(_S())
    meter(Usage(model="zzz", operation="chat", input_tokens=1000))
    assert Decimal(meter.snapshot()["total"]["cost_usd"]) == Decimal("0.5")


def test_from_settings_without_override_uses_default_table() -> None:
    class _S:
        optim_pricing_json = None

    meter = CostMeter.from_settings(_S())
    meter(Usage(model="qwen3.7-max", operation="chat", input_tokens=1000, output_tokens=1000))
    assert Decimal(meter.snapshot()["total"]["cost_usd"]) > Decimal("0")
