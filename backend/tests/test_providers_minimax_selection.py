"""Config + selection + budget-math tests for the MiniMax video backend."""

from __future__ import annotations

from app.core.config import Settings
from app.providers import create_providers
from app.providers.minimax import MiniMaxVideoProvider
from app.providers.video import VideoProvider


def _settings(**overrides: object) -> Settings:
    return Settings(dashscope_api_key="test", **overrides)  # type: ignore[arg-type]


def test_minimax_config_defaults() -> None:
    s = _settings()
    assert s.video_backend == "dashscope"
    assert s.minimax_api_key is None
    assert s.minimax_base_url == "https://api.minimax.io/v1"
    assert s.minimax_video_model == "MiniMax-Hailuo-2.3-Fast"
    assert s.minimax_resolution == "768P"
    assert s.minimax_duration_s == 6
    assert s.minimax_cost_per_clip_usd == 0.19
    assert s.budget_ceiling_usd == 30.0


def test_minimax_config_overrides() -> None:
    s = _settings(
        video_backend="minimax",
        minimax_api_key="sk-mm",
        minimax_cost_per_clip_usd=0.08,
        budget_ceiling_usd=10.0,
    )
    assert s.video_backend == "minimax"
    assert s.minimax_api_key == "sk-mm"
    assert s.minimax_cost_per_clip_usd == 0.08
    assert s.budget_ceiling_usd == 10.0


def test_create_providers_default_is_wan() -> None:
    providers = create_providers(_settings())
    assert isinstance(providers.video, VideoProvider)


def test_create_providers_minimax_backend_selected() -> None:
    providers = create_providers(
        _settings(video_backend="minimax", minimax_api_key="sk-mm")
    )
    assert isinstance(providers.video, MiniMaxVideoProvider)
    assert providers.video.name == "minimax:MiniMax-Hailuo-2.3-Fast"


def test_minimax_backend_without_key_falls_back_to_wan() -> None:
    # Misconfiguration guard: selecting minimax with no key must not crash the
    # whole provider bundle at construction; fall back to Wan (which still gates
    # spend) and let preflight surface the missing key.
    providers = create_providers(_settings(video_backend="minimax", minimax_api_key=None))
    assert isinstance(providers.video, VideoProvider)


def test_budget_seconds_equivalent_of_thirty_dollars() -> None:
    # $30 / $0.19 per clip * 6s/clip ≈ 947s ≈ 157 whole clips. Assert the
    # documented mapping the operator sets BUDGET_CEILING_VIDEO_S to for a MiniMax
    # run.  int() (floor) is used because you can only buy whole clips.
    s = _settings()
    clips = s.budget_ceiling_usd / s.minimax_cost_per_clip_usd
    seconds = clips * s.minimax_duration_s
    assert int(clips) == 157
    assert int(seconds) == 947
