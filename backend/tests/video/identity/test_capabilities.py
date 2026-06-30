"""CapabilityProfile + profile_for resolution — declarative, deterministic."""

from __future__ import annotations

from app.video.identity import (
    DEFAULT_PROFILE,
    CapabilityProfile,
    ConditioningKind,
    ImageTransport,
    profile_for,
)
from app.video.identity.capabilities import FIDELITY_RANK


def test_profile_for_classifies_real_backend_names() -> None:
    assert profile_for("video:wan2.1-i2v-turbo").name == "wan-i2v"
    assert profile_for("video:wan2.2-i2v-plus").name == "wan-i2v"
    assert profile_for("video:wan2.5-t2v-preview").name == "wan-t2v"
    assert profile_for("video:wan2.1-t2v-turbo").name == "wan-t2v"
    assert profile_for("minimax:MiniMax-Hailuo-2.3-Fast").name == "minimax"
    assert profile_for("video-router-hailuo").name == "minimax"


def test_r2v_token_routes_to_reference_set_profile() -> None:
    prof = profile_for("video:wan-r2v-custom")
    assert prof.name == "wan-r2v"
    assert prof.supports(ConditioningKind.REFERENCE_SET)
    assert prof.accepts_reference_voice is True
    assert prof.max_reference_images == 3


def test_unknown_backend_resolves_to_default_first_frame() -> None:
    prof = profile_for("some-future-model-x")
    assert prof is DEFAULT_PROFILE
    assert prof.supports(ConditioningKind.FIRST_FRAME)
    assert prof.best_supported() is ConditioningKind.FIRST_FRAME


def test_t2v_profile_has_no_direct_reference() -> None:
    t2v = profile_for("video:wan2.1-t2v-turbo")
    assert t2v.supported == frozenset({ConditioningKind.NONE})
    assert t2v.has_direct_reference is False
    assert t2v.best_supported() is ConditioningKind.NONE


def test_minimax_transport_is_base64() -> None:
    mm = profile_for("minimax")
    assert mm.image_transport is ImageTransport.BASE64
    assert mm.supports(ConditioningKind.INLINE_IMAGE)
    assert mm.has_direct_reference is True


def test_best_supported_picks_highest_fidelity() -> None:
    prof = CapabilityProfile(
        name="multi",
        supported=frozenset(
            {ConditioningKind.FIRST_FRAME, ConditioningKind.REFERENCE_SET}
        ),
    )
    assert prof.best_supported() is ConditioningKind.REFERENCE_SET
    # empty support → NONE
    empty = CapabilityProfile(name="empty", supported=frozenset())
    assert empty.best_supported() is ConditioningKind.NONE


def test_fidelity_rank_orders_reference_above_first_frame() -> None:
    assert FIDELITY_RANK[ConditioningKind.REFERENCE_SET] > FIDELITY_RANK[
        ConditioningKind.FIRST_FRAME
    ]
    assert FIDELITY_RANK[ConditioningKind.NONE] == 0
