"""Keyframe fallback — bake, reuse, no-baker, and degradation paths."""

from __future__ import annotations

from app.video.identity import (
    IdentityBundle,
    KeyframeFallback,
    KeyframeSource,
    Pose,
    build_bake_prompt,
)
from app.video.identity.keyframe import FallbackConfig

from .conftest import FakeBaker, make_bundle


async def test_bake_path_synthesises_keyframe_conditioned_on_refs() -> None:
    baker = FakeBaker()
    fb = KeyframeFallback(baker=baker)
    bundle = make_bundle()
    kf = await fb.keyframe_for(bundle, shot_prompt="at the frozen window", desired_pose=Pose.FRONT)
    assert kf is not None
    assert kf.source is KeyframeSource.BAKED
    assert kf.image_bytes == baker.output
    assert kf.pose is Pose.FRONT
    # Conditioned on the locked refs that carry bytes (capped at 3).
    assert baker.calls[0].n_refs == 3
    # Appearance phrase leads the bake prompt; negatives passed through.
    assert "platinum braid" in baker.calls[0].prompt
    assert baker.calls[0].negative_prompt == "warped face, extra fingers"


async def test_bake_failure_degrades_to_reuse() -> None:
    fb = KeyframeFallback(baker=FakeBaker(fail=True))
    bundle = make_bundle()
    kf = await fb.keyframe_for(bundle, desired_pose=Pose.FRONT)
    assert kf is not None
    assert kf.source is KeyframeSource.REUSED_REFERENCE
    # reused the best byte-carrying front ref
    assert kf.source_ref_ids == ("char_elsa@v3:front",)


async def test_empty_bake_degrades_to_reuse() -> None:
    fb = KeyframeFallback(baker=FakeBaker(empty=True))
    kf = await fb.keyframe_for(make_bundle(), desired_pose=Pose.PROFILE)
    assert kf is not None
    assert kf.source is KeyframeSource.REUSED_REFERENCE
    assert kf.pose is Pose.PROFILE  # reused the profile ref for a profile shot


async def test_no_baker_reuses_best_locked_ref() -> None:
    fb = KeyframeFallback()  # no baker
    assert fb.can_bake is False
    kf = await fb.keyframe_for(make_bundle(), desired_pose=Pose.FRONT)
    assert kf is not None
    assert kf.source is KeyframeSource.REUSED_REFERENCE


async def test_no_baker_no_bytes_returns_none() -> None:
    fb = KeyframeFallback()
    # URL-only bundle: nothing to reuse as a byte keyframe.
    bundle = make_bundle(with_bytes=False)
    assert await fb.keyframe_for(bundle) is None


async def test_keyframe_as_locked_reference_is_unlocked() -> None:
    fb = KeyframeFallback(baker=FakeBaker())
    kf = await fb.keyframe_for(make_bundle(), desired_pose=Pose.FRONT)
    assert kf is not None
    ref = kf.as_locked_reference()
    assert ref.locked is False
    assert ref.has_bytes is True
    assert ref.pose is Pose.FRONT


async def test_bake_uses_seed_and_caps_conditioning_refs() -> None:
    baker = FakeBaker()
    fb = KeyframeFallback(baker=baker, config=FallbackConfig(max_conditioning_refs=1))
    kf = await fb.keyframe_for(make_bundle(), desired_pose=Pose.FRONT, seed=4242)
    assert kf is not None
    assert kf.seed == 4242
    assert baker.calls[0].n_refs == 1
    assert baker.calls[0].seed == 4242


def test_build_bake_prompt_orders_identity_first() -> None:
    bundle = make_bundle()
    prompt = build_bake_prompt(bundle, "snow drifting", pose=Pose.THREE_QUARTER)
    assert prompt.startswith("platinum braid")
    assert "snow drifting" in prompt
    assert prompt.endswith("3q view")


def test_build_bake_prompt_uses_name_when_no_phrase() -> None:
    bundle = IdentityBundle(entity_key="e", entity_type="character", name="Kai")
    prompt = build_bake_prompt(bundle, "a shot", pose=Pose.UNKNOWN)
    assert prompt.startswith("Kai")
    assert "view" not in prompt  # UNKNOWN pose adds no cue
