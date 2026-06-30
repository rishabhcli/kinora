"""Unit tests for the canonical request + the WanSpec adapter + the settings bridge.

The settings bridge is validated against the *real* :class:`Settings` defaults so
the layer stays in lock-step with config (MiniMax flat $0.19/clip, Wan free tier =
budget_ceiling_video_s, $30 USD global cap). No infra.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.video.cost.money import Currency, Money
from app.video.cost.request import VideoCostRequest, VideoMode, from_wan_spec


def test_request_validation() -> None:
    with pytest.raises(ValueError):
        VideoCostRequest(duration_s=-1)
    with pytest.raises(ValueError):
        VideoCostRequest(duration_s=6, fps=0)


def test_request_derived_fields() -> None:
    req = VideoCostRequest(duration_s=2, fps=24, resolution="720p")
    assert req.resolution_tier == "720P"
    assert req.frame_count == 48
    assert req.with_duration(5).duration_s == 5
    assert req.with_resolution("1080P").resolution == "1080P"


def test_from_wan_spec_maps_mode_and_dims() -> None:
    from app.providers.types import WanMode, WanSpec

    spec = WanSpec(mode=WanMode.IMAGE_TO_VIDEO, duration_s=5, resolution="720P", shot_id="shot-1")
    req = from_wan_spec(spec, session_id="s1", book_id="b1")
    assert req.mode is VideoMode.IMAGE_TO_VIDEO
    assert req.duration_s == 5.0
    assert req.shot_id == "shot-1"
    assert req.session_id == "s1" and req.book_id == "b1"


# --------------------------------------------------------------------------- #
# Settings bridge against the real Settings defaults
# --------------------------------------------------------------------------- #


def _settings():  # type: ignore[no-untyped-def]
    from app.core.config import Settings

    return Settings(dashscope_api_key="test")


def test_registry_from_settings_matches_config_defaults() -> None:
    from app.video.cost.settings import registry_from_settings

    s = _settings()
    reg = registry_from_settings(s)
    minimax = reg.get("minimax", s.minimax_video_model)
    # The flat per-clip price equals the configured per-clip USD.
    assert minimax.price(VideoCostRequest(duration_s=s.minimax_duration_s)) == Money.from_float(
        s.minimax_cost_per_clip_usd, Currency.USD
    )
    wan = reg.get("dashscope", s.video_model)
    assert wan.free_tier_seconds == int(s.budget_ceiling_video_s)


def test_caps_from_settings_global_is_usd_ceiling() -> None:
    from app.video.cost.settings import caps_from_settings

    s = _settings()
    caps = caps_from_settings(s, per_provider_usd={"minimax": "10.00"}, per_book_usd="5.00")
    assert caps.global_cap == Money.from_float(s.budget_ceiling_usd, Currency.USD)
    assert caps.per_provider["minimax"] == Money.usd("10.00")
    assert caps.per_book == Money.usd("5.00")


async def test_cost_layer_end_to_end() -> None:
    from app.video.cost.enforcement import CapabilityCandidate, cheapest_capable
    from app.video.cost.ledger import SpendScope
    from app.video.cost.settings import cost_layer_from_settings

    s = _settings()
    layer = cost_layer_from_settings(s)
    req = VideoCostRequest(duration_s=6, resolution="768P", book_id="b1", session_id="s1")
    cands = [
        CapabilityCandidate("minimax", s.minimax_video_model),
        CapabilityCandidate("dashscope", s.video_model),
    ]
    # Free tier intact -> Wan wins (0.00).
    choice = await cheapest_capable(req, cands, estimator=layer.estimator, enforcer=layer.enforcer)
    assert choice is not None and choice.provider == "dashscope"

    # Reserve, then commit at an actual, then reconcile drift.
    scope = SpendScope(provider="minimax", book_id="b1", session_id="s1")
    est = layer.estimator.estimate(req, "minimax", s.minimax_video_model)
    res = await layer.enforcer.reserve(est.expected, scope)
    actual = Money.usd("0.20")
    await layer.enforcer.commit(res, actual)
    layer.drift.record_estimate_actual("minimax", s.minimax_video_model, est.expected, actual)
    assert layer.drift.by_provider()["minimax"].under_estimating is True
    assert (await layer.enforcer.remaining_global()) < Money.from_float(
        s.budget_ceiling_usd, Currency.USD
    )


def test_cost_layer_per_book_cap_blocks_overrun() -> None:
    # Verified via the enforcer wired from settings with a tiny per-book cap.
    from app.video.cost.settings import cost_layer_from_settings

    s = _settings()
    layer = cost_layer_from_settings(s, per_book_usd="0.10")
    assert layer.enforcer.caps.per_book == Money.usd("0.10")
    assert layer.enforcer.caps.soft_cap_fraction == Decimal("0.90")
