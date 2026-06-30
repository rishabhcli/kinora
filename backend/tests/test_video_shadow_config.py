"""Additive shadow settings: safe defaults + adaptation from a live Settings."""

from __future__ import annotations

import pytest

from app.video.shadow.config import ShadowSettings, shadow_settings_from


def test_defaults_are_safe() -> None:
    cfg = ShadowSettings()
    assert cfg.enabled is False
    assert cfg.sample_fraction == 0.0
    assert cfg.eval_video_seconds == 0.0
    assert cfg.is_live_funded is False


def test_fraction_bounds_enforced() -> None:
    with pytest.raises(ValueError):
        ShadowSettings(sample_fraction=1.5)
    with pytest.raises(ValueError):
        ShadowSettings(sample_fraction=-0.1)


def test_is_live_funded_requires_positive_seconds() -> None:
    assert ShadowSettings(eval_video_seconds=10.0).is_live_funded
    assert not ShadowSettings(eval_video_seconds=0.0).is_live_funded


def test_adapts_from_object_with_attributes() -> None:
    class FakeSettings:
        video_shadow_enabled = True
        video_shadow_sample_fraction = 0.25
        video_shadow_sample_salt = "cand-x"
        video_shadow_eval_video_seconds = 50.0
        video_shadow_candidate_model = "wan2.5-t2v-preview"
        video_shadow_confidence = 0.99
        video_shadow_win_margin = 0.02

    cfg = shadow_settings_from(FakeSettings())
    assert cfg.enabled is True
    assert cfg.sample_fraction == 0.25
    assert cfg.candidate_model == "wan2.5-t2v-preview"
    assert cfg.confidence == 0.99
    assert cfg.is_live_funded


def test_adapts_with_missing_attributes_falls_back_to_safe() -> None:
    class Bare:
        pass

    cfg = shadow_settings_from(Bare())
    assert cfg.enabled is False
    assert cfg.sample_fraction == 0.0
    assert cfg.eval_video_seconds == 0.0


def test_reads_from_real_settings_defaults() -> None:
    # The central Settings carries the additive video_shadow_* fields, all safe.
    from app.core.config import get_settings

    cfg = shadow_settings_from(get_settings())
    assert cfg.enabled is False
    assert cfg.sample_fraction == 0.0
    assert cfg.eval_video_seconds == 0.0
