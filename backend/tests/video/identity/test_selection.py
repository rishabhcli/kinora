"""Reference-image selection / ranking policy — pose-aware, deterministic."""

from __future__ import annotations

from app.video.identity import (
    IdentityBundle,
    LockedReference,
    Pose,
    SelectionPolicy,
    desired_pose_for,
    pose_affinity,
    rank_references,
    select_best,
    select_top,
)

from .conftest import PNG, make_bundle


def test_desired_pose_for_shot_size() -> None:
    assert desired_pose_for("closeup") is Pose.CLOSEUP
    assert desired_pose_for("medium") is Pose.THREE_QUARTER
    assert desired_pose_for("wide") is Pose.FULL_BODY
    assert desired_pose_for("establishing") is Pose.ESTABLISHING
    assert desired_pose_for(None) is Pose.FRONT
    assert desired_pose_for("weird") is Pose.FRONT


def test_pose_affinity_exact_and_symmetric() -> None:
    assert pose_affinity(Pose.FRONT, Pose.FRONT) == 1.0
    # symmetric: front<->3q same either way
    assert pose_affinity(Pose.FRONT, Pose.THREE_QUARTER) == pose_affinity(
        Pose.THREE_QUARTER, Pose.FRONT
    )
    # unknown is a neutral mid
    assert pose_affinity(Pose.FRONT, Pose.UNKNOWN) == 0.5
    # front matches profile worse than 3q
    assert pose_affinity(Pose.FRONT, Pose.PROFILE) < pose_affinity(
        Pose.FRONT, Pose.THREE_QUARTER
    )


def test_select_best_matches_desired_pose() -> None:
    bundle = make_bundle()
    # profile shot → the profile ref wins.
    best = select_best(bundle, desired_pose=Pose.PROFILE)
    assert best is not None and best.pose is Pose.PROFILE
    # front shot → the front ref wins.
    front = select_best(bundle, desired_pose=Pose.FRONT)
    assert front is not None and front.pose is Pose.FRONT


def test_select_top_respects_k_and_order() -> None:
    bundle = make_bundle()
    top2 = select_top(bundle, k=2, desired_pose=Pose.FRONT)
    assert len(top2) == 2
    assert top2[0].pose is Pose.FRONT  # best for a front shot
    assert select_top(bundle, k=0) == []
    assert len(select_top(bundle, k=99)) == 3  # capped at available


def test_ranking_is_stable_for_ties() -> None:
    # Two refs identical in pose+quality → input order preserved.
    a = LockedReference(ref_id="a", pose=Pose.FRONT, url="u", quality=0.9)
    b = LockedReference(ref_id="b", pose=Pose.FRONT, url="u", quality=0.9)
    bundle = IdentityBundle(
        entity_key="e", entity_type="character", name="E", references=(a, b)
    )
    ranked = rank_references(bundle, desired_pose=Pose.FRONT)
    assert [r.reference.ref_id for r in ranked] == ["a", "b"]


def test_quality_breaks_pose_ties() -> None:
    hi = LockedReference(ref_id="hi", pose=Pose.FRONT, url="u", quality=0.99)
    lo = LockedReference(ref_id="lo", pose=Pose.FRONT, url="u", quality=0.10)
    bundle = IdentityBundle(
        entity_key="e", entity_type="character", name="E", references=(lo, hi)
    )
    ranked = rank_references(bundle, desired_pose=Pose.FRONT)
    assert ranked[0].reference.ref_id == "hi"


def test_require_bytes_filters_url_only_refs() -> None:
    byte_ref = LockedReference(ref_id="bytes", pose=Pose.FRONT, image_bytes=PNG)
    url_ref = LockedReference(ref_id="urlonly", pose=Pose.FRONT, url="https://oss/x.png")
    bundle = IdentityBundle(
        entity_key="e", entity_type="character", name="E", references=(url_ref, byte_ref)
    )
    ranked = rank_references(bundle, desired_pose=Pose.FRONT, require_bytes=True)
    assert [r.reference.ref_id for r in ranked] == ["bytes"]
    best = select_best(bundle, require_bytes=True)
    assert best is not None and best.ref_id == "bytes"


def test_select_best_empty_bundle_is_none() -> None:
    empty = IdentityBundle(entity_key="e", entity_type="character", name="E")
    assert select_best(empty) is None


def test_descriptor_bonus_breaks_ties() -> None:
    with_desc = LockedReference(
        ref_id="d", pose=Pose.FRONT, url="u", quality=0.5, descriptor=(1.0, 0.0)
    )
    without = LockedReference(ref_id="n", pose=Pose.FRONT, url="u", quality=0.5)
    bundle = IdentityBundle(
        entity_key="e", entity_type="character", name="E", references=(without, with_desc)
    )
    policy = SelectionPolicy(prefer_descriptor=True)
    ranked = rank_references(bundle, desired_pose=Pose.FRONT, policy=policy)
    assert ranked[0].reference.ref_id == "d"
