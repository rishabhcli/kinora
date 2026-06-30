"""IdentityConditioner — every conditioning path + WanSpec application.

Covers the full strategy ladder against the repo's real backend profiles:
reference-set (Wan r2v), character-id, inline-image (MiniMax base64), first-frame
/ first-last-frame, and the keyframe fallback for a t2v-only backend.
"""

from __future__ import annotations

import base64

from app.providers.types import WanMode, WanSpec
from app.video.identity import (
    CapabilityProfile,
    ConditionerConfig,
    ConditioningKind,
    IdentityConditioner,
    ImageTransport,
    KeyframeFallback,
    KeyframeSource,
    Pose,
    profile_for,
)

from .conftest import FakeBaker, make_bundle


def _spec(
    mode: WanMode = WanMode.REFERENCE_TO_VIDEO, prompt: str = "Elsa at the window"
) -> WanSpec:
    return WanSpec(mode=mode, prompt=prompt)


# --------------------------------------------------------------------------- #
# Reference-set (Wan r2v) — the strongest path
# --------------------------------------------------------------------------- #


async def test_reference_set_emits_urls_and_voice() -> None:
    cond = IdentityConditioner()
    bundle = make_bundle()
    profile = profile_for("video:wan-r2v")
    plan = await cond.plan(
        bundle, profile, requested_mode=WanMode.REFERENCE_TO_VIDEO, desired_pose=Pose.FRONT
    )
    assert plan.kind is ConditioningKind.REFERENCE_SET
    assert plan.mode is WanMode.REFERENCE_TO_VIDEO
    # capped at the profile's max (3) and front-best first.
    assert len(plan.reference_values) == 3
    assert plan.reference_values[0] == "https://oss/elsa/front.png"
    assert plan.voice_ref_url == "https://oss/elsa/voice.wav"
    assert "char_elsa@v3:front" in plan.selected_ref_ids

    spec = _spec()
    plan.apply_to(spec)
    assert spec.mode is WanMode.REFERENCE_TO_VIDEO
    assert spec.reference_image_urls == list(plan.reference_values)
    assert spec.reference_voice_url == "https://oss/elsa/voice.wav"
    # appearance phrase appended; anti-drift negatives merged.
    assert "platinum braid" in spec.prompt
    assert "warped face" in (spec.negative_prompt or "")


async def test_reference_set_respects_max_reference_images() -> None:
    cond = IdentityConditioner()
    profile = CapabilityProfile(
        name="r2v-1",
        supported=frozenset({ConditioningKind.REFERENCE_SET}),
        max_reference_images=1,
    )
    plan = await cond.plan(make_bundle(), profile, desired_pose=Pose.PROFILE)
    assert len(plan.reference_values) == 1
    # profile shot → profile ref selected as the single best.
    assert plan.selected_ref_ids == ("char_elsa@v3:profile",)


# --------------------------------------------------------------------------- #
# Character-id
# --------------------------------------------------------------------------- #


async def test_character_id_path() -> None:
    cond = IdentityConditioner()
    bundle = make_bundle(with_character_id=True)
    profile = CapabilityProfile(
        name="ip", supported=frozenset({ConditioningKind.CHARACTER_ID})
    )
    plan = await cond.plan(bundle, profile)
    assert plan.kind is ConditioningKind.CHARACTER_ID
    assert plan.character_id == "ipadapter_elsa_8f2a"
    assert plan.mode is WanMode.IMAGE_TO_VIDEO


async def test_character_id_skipped_when_bundle_has_none() -> None:
    cond = IdentityConditioner()
    bundle = make_bundle(with_character_id=False)  # no id → fall through
    profile = CapabilityProfile(
        name="ip+ff",
        supported=frozenset({ConditioningKind.CHARACTER_ID, ConditioningKind.FIRST_FRAME}),
        max_reference_images=1,
    )
    plan = await cond.plan(bundle, profile)
    assert plan.kind is ConditioningKind.FIRST_FRAME  # fell through to first-frame


# --------------------------------------------------------------------------- #
# Inline-image (MiniMax base64)
# --------------------------------------------------------------------------- #


async def test_inline_image_emits_base64_for_minimax() -> None:
    cond = IdentityConditioner()
    bundle = make_bundle()
    profile = profile_for("minimax")
    assert profile.image_transport is ImageTransport.BASE64
    plan = await cond.plan(bundle, profile, desired_pose=Pose.FRONT)
    # MiniMax supports both FIRST_FRAME and INLINE_IMAGE; INLINE_IMAGE wins (higher fidelity).
    assert plan.kind is ConditioningKind.INLINE_IMAGE
    assert plan.image_value is not None
    # value is raw base64 (decodes back to the front PNG bytes).
    decoded = base64.b64decode(plan.image_value)
    assert decoded.startswith(b"\x89PNG")
    spec = _spec(mode=WanMode.IMAGE_TO_VIDEO)
    plan.apply_to(spec)
    assert spec.image_url == plan.image_value


async def test_inline_image_needs_bytes_else_falls_through() -> None:
    cond = IdentityConditioner()
    bundle = make_bundle(with_bytes=False)  # url-only
    profile = CapabilityProfile(
        name="inline-only",
        supported=frozenset({ConditioningKind.INLINE_IMAGE}),
        image_transport=ImageTransport.BASE64,
    )
    # No bytes, no other supported kind, no baker → text-only fallback.
    plan = await cond.plan(bundle, profile)
    assert plan.kind is ConditioningKind.NONE
    assert plan.used_fallback is True


# --------------------------------------------------------------------------- #
# First-frame / first-last-frame (Wan i2v)
# --------------------------------------------------------------------------- #


async def test_first_frame_path_for_wan_i2v() -> None:
    cond = IdentityConditioner()
    bundle = make_bundle()
    profile = profile_for("video:wan2.1-i2v-turbo")
    plan = await cond.plan(
        bundle, profile, requested_mode=WanMode.IMAGE_TO_VIDEO, desired_pose=Pose.FRONT
    )
    assert plan.kind is ConditioningKind.FIRST_FRAME
    assert plan.mode is WanMode.IMAGE_TO_VIDEO
    assert plan.image_value == "https://oss/elsa/front.png"  # URL transport
    spec = _spec(mode=WanMode.IMAGE_TO_VIDEO)
    plan.apply_to(spec)
    assert spec.image_url == "https://oss/elsa/front.png"


async def test_first_last_frame_honoured_when_requested_and_supported() -> None:
    cond = IdentityConditioner()
    profile = profile_for("video:wan2.1-i2v-turbo")  # supports FLF
    plan = await cond.plan(
        make_bundle(), profile, requested_mode=WanMode.FIRST_LAST_FRAME, desired_pose=Pose.FRONT
    )
    assert plan.kind is ConditioningKind.FIRST_LAST_FRAME
    assert plan.mode is WanMode.FIRST_LAST_FRAME
    spec = _spec(mode=WanMode.FIRST_LAST_FRAME)
    plan.apply_to(spec)
    assert spec.first_frame_url == "https://oss/elsa/front.png"
    assert spec.image_url is None


# --------------------------------------------------------------------------- #
# Keyframe fallback (t2v-only backend)
# --------------------------------------------------------------------------- #


async def test_t2v_only_backend_uses_keyframe_fallback_with_baker() -> None:
    baker = FakeBaker()
    cond = IdentityConditioner(fallback=KeyframeFallback(baker=baker))
    bundle = make_bundle()
    profile = profile_for("video:wan2.1-t2v-turbo")  # NONE only
    plan = await cond.plan(
        bundle,
        profile,
        requested_mode=WanMode.TEXT_TO_VIDEO,
        shot_prompt="snow",
        desired_pose=Pose.FRONT,
    )
    # Re-routed to image-to-video, driven by the baked keyframe.
    assert plan.used_fallback is True
    assert plan.kind is ConditioningKind.FIRST_FRAME
    assert plan.mode is WanMode.IMAGE_TO_VIDEO
    assert plan.baked_keyframe is not None
    assert plan.baked_keyframe.source is KeyframeSource.BAKED
    assert baker.calls  # the baker actually ran
    spec = _spec(mode=WanMode.TEXT_TO_VIDEO)
    plan.apply_to(spec)
    assert spec.mode is WanMode.IMAGE_TO_VIDEO  # mode was re-routed
    assert spec.image_url is not None and spec.image_url.startswith("data:")


async def test_t2v_only_no_baker_reuses_ref_keyframe() -> None:
    cond = IdentityConditioner(fallback=KeyframeFallback())  # no baker
    bundle = make_bundle()
    profile = profile_for("video:wan2.5-t2v-preview")
    plan = await cond.plan(bundle, profile, requested_mode=WanMode.TEXT_TO_VIDEO)
    assert plan.used_fallback is True
    assert plan.kind is ConditioningKind.FIRST_FRAME
    assert plan.baked_keyframe is not None
    assert plan.baked_keyframe.source is KeyframeSource.REUSED_REFERENCE


async def test_t2v_only_no_bytes_no_baker_text_only() -> None:
    cond = IdentityConditioner(fallback=KeyframeFallback())
    bundle = make_bundle(with_bytes=False)  # url-only, nothing to bake/reuse as bytes
    profile = profile_for("video:wan2.1-t2v-turbo")
    plan = await cond.plan(bundle, profile, requested_mode=WanMode.TEXT_TO_VIDEO)
    assert plan.kind is ConditioningKind.NONE
    assert plan.mode is WanMode.TEXT_TO_VIDEO
    assert plan.used_fallback is True
    spec = _spec(mode=WanMode.TEXT_TO_VIDEO)
    plan.apply_to(spec)
    # identity still reinforced textually.
    assert "platinum braid" in spec.prompt


# --------------------------------------------------------------------------- #
# Cross-cutting behaviour
# --------------------------------------------------------------------------- #


async def test_default_profile_unknown_backend_uses_first_frame() -> None:
    cond = IdentityConditioner()
    plan = await cond.plan(make_bundle(), profile_for("mystery-model"))
    assert plan.kind is ConditioningKind.FIRST_FRAME
    assert plan.image_value == "https://oss/elsa/front.png"


async def test_negative_prompt_not_injected_when_backend_rejects_it() -> None:
    cond = IdentityConditioner()
    profile = CapabilityProfile(
        name="no-neg",
        supported=frozenset({ConditioningKind.REFERENCE_SET}),
        max_reference_images=2,
        accepts_negative_prompt=False,
    )
    plan = await cond.plan(make_bundle(), profile)
    assert plan.negative_suffix == ""
    spec = _spec()
    spec.negative_prompt = None
    plan.apply_to(spec)
    assert spec.negative_prompt is None


async def test_seed_pinned_only_when_spec_has_none() -> None:
    cond = IdentityConditioner()
    profile = profile_for("video:wan-r2v")
    plan = await cond.plan(make_bundle(), profile, seed=777)
    assert plan.seed == 777
    spec = _spec()
    spec.seed = 123  # caller already chose a seed → not overwritten
    plan.apply_to(spec)
    assert spec.seed == 123
    spec2 = _spec()
    plan.apply_to(spec2)
    assert spec2.seed == 777


async def test_reinforcement_can_be_disabled() -> None:
    cond = IdentityConditioner(
        config=ConditionerConfig(reinforce_prompt=False, reinforce_negatives=False)
    )
    plan = await cond.plan(make_bundle(), profile_for("video:wan-r2v"))
    assert plan.prompt_suffix == ""
    assert plan.negative_suffix == ""
    spec = _spec(prompt="bare prompt")
    plan.apply_to(spec)
    assert spec.prompt == "bare prompt"


async def test_voice_only_for_voice_accepting_backends() -> None:
    cond = IdentityConditioner()
    # i2v does NOT accept reference voice.
    plan = await cond.plan(make_bundle(), profile_for("video:wan2.1-i2v-turbo"))
    assert plan.voice_ref_url is None
