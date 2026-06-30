"""Adaptive-scheduler v2 settings tests (kinora.md §4.5/§4.6) — additive + off."""

from __future__ import annotations

from app.core.config import get_settings


def test_v2_is_off_by_default() -> None:
    s = get_settings()
    # The opt-in flag is OFF by default so the live scheduler is byte-for-byte today.
    assert s.scheduler_v2_enabled is False


def test_v2_defaults_are_sane() -> None:
    s = get_settings()
    assert s.scheduler_v2_skim_ceiling_multiple > 0.0
    assert 0.0 < s.scheduler_v2_reread_backward_fraction < 1.0
    assert s.scheduler_v2_ponder_dwell_ms > 0.0
    assert s.scheduler_v2_regime_min_samples >= 1
    assert s.scheduler_v2_provider_latency_s > 0.0
    assert s.scheduler_v2_max_parallel_promotions >= 0  # 0 = "no hard cap"
    assert s.scheduler_v2_prefetch_depth >= 0
    assert s.scheduler_v2_cold_cache_capacity >= 1


def test_v2_settings_do_not_touch_the_live_video_gate() -> None:
    s = get_settings()
    # Nothing in the v2 block can flip the spend gate.
    assert s.kinora_live_video is False
