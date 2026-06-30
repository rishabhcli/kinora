"""Per-provider transcode-profile resolution + IDR-alignment normalization."""

from __future__ import annotations

import pytest

from app.delivery.errors import ProfileError
from app.delivery.profiles import (
    DEFAULT_PROFILE_KEY,
    PROVIDER_PROFILES,
    TARGET_VIDEO_CODEC,
    normalization_spec,
    profile_for,
)


def test_profile_for_exact_keys() -> None:
    for key in ("wan_dashscope", "minimax", "ken_burns", "wan_local"):
        assert profile_for(key).key == key


def test_profile_for_model_id_families() -> None:
    # Wan model ids and MiniMax variants resolve via substring heuristics.
    assert profile_for("wan2.1-i2v-turbo").key == "wan_dashscope"
    assert profile_for("wan2.2-i2v-plus").key == "wan_dashscope"
    assert profile_for("wan-local-ti2v-5b").key == "wan_local"
    assert profile_for("minimax-hailuo-02").key == "minimax"
    assert profile_for("ken-burns-degrade").key == "ken_burns"


def test_profile_for_unknown_and_none_falls_back() -> None:
    assert profile_for(None).key == DEFAULT_PROFILE_KEY
    assert profile_for("brand-new-model-9000").key == DEFAULT_PROFILE_KEY
    assert profile_for("   ").key == DEFAULT_PROFILE_KEY


def test_generative_providers_always_need_full_transcode() -> None:
    # No generative provider is copy-safe — their GOP/keyframe cadence is unreliable.
    for key in ("wan_dashscope", "minimax", "wan_local", "ken_burns"):
        assert PROVIDER_PROFILES[key].needs_full_transcode is True


def test_normalization_spec_gop_divides_segment_for_idr_alignment() -> None:
    profile = profile_for("minimax")
    spec = normalization_spec(profile, fps=30, segment_duration_s=2.0)
    assert spec.gop_size == 60  # 30fps * 2s
    assert spec.target_codec == TARGET_VIDEO_CODEC
    assert spec.full_transcode is True
    assert "n_forced" in spec.force_keyframe_expr


def test_normalization_spec_rejects_nonintegral_gop() -> None:
    # 30fps * 1.7s = 51 → integral, OK; but 30 * 0.05 = 1.5 → not integral.
    profile = profile_for("wan_dashscope")
    with pytest.raises(ProfileError):
        normalization_spec(profile, fps=30, segment_duration_s=0.05)


def test_normalization_spec_rejects_nonpositive() -> None:
    profile = profile_for("wan_dashscope")
    with pytest.raises(ProfileError):
        normalization_spec(profile, fps=0, segment_duration_s=2.0)
    with pytest.raises(ProfileError):
        normalization_spec(profile, fps=30, segment_duration_s=0)


def test_normalization_spec_fps_segment_combos() -> None:
    # 24fps * 2.5s = 60; 25fps * 4s = 100 — both integral, both align.
    profile = profile_for("minimax")
    assert normalization_spec(profile, fps=24, segment_duration_s=2.5).gop_size == 60
    assert normalization_spec(profile, fps=25, segment_duration_s=4.0).gop_size == 100
