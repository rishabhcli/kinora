"""Reference-image / identity conditioning, normalized across every provider.

Kinora's core promise is *visual consistency across a long adaptation*: a
character or setting must look the same regardless of which video model renders a
given shot (kinora.md §0, §6 "lock identity", §9.5 character-consistency score).
Providers, though, accept locked identity in incompatible shapes — a reference
image set (Wan r2v), a single first frame (image-to-video), inline base64, a
registered subject / IP-Adapter id, or *nothing at all* (pure text-to-video).

This package normalizes that:

* :class:`IdentityBundle` — one entity's locked visual truth (ranked reference
  images + appearance descriptor + style centroid + optional subject id / voice),
  assembled from the canon graph via :func:`bundle_from_canon_slice`.
* :class:`CapabilityProfile` — a small *local* declaration of what one backend can
  ingest; :func:`profile_for` resolves the repo's real backends + a safe default.
* :class:`IdentityConditioner` — given a bundle + a capability profile + the shot's
  framing, chooses the highest-fidelity conditioning strategy the backend supports
  and emits provider-appropriate fields (:class:`ConditioningPlan`, applied to a
  :class:`~app.providers.types.WanSpec`).
* :class:`KeyframeFallback` — for backends that take no reference, bakes (or reuses)
  a keyframe pinning the locked identity and re-routes the shot to image-to-video.
* Reference-image selection/ranking (:func:`select_best` / :func:`select_top`) — a
  pose/framing-aware policy that picks the best locked ref for a shot.
* :class:`IdentitySelfCheck` / :func:`score_descriptor` — the closing-the-loop hook
  that scores an output crop's drift against the locked identity.

Everything here is additive, deterministic, and provider-agnostic: the only point
that touches the provider layer is :meth:`ConditioningPlan.apply_to`.
"""

from __future__ import annotations

from .assembler import bundle_from_canon_slice, locked_reference_from_ref_image
from .bundle import IdentityBundle, LockedReference, Pose, centroid, cosine
from .capabilities import (
    DEFAULT_PROFILE,
    KNOWN_PROFILES,
    CapabilityProfile,
    ConditioningKind,
    ImageTransport,
    profile_for,
)
from .conditioner import ConditionerConfig, ConditioningPlan, IdentityConditioner
from .keyframe import (
    BakedKeyframe,
    FallbackConfig,
    KeyframeBaker,
    KeyframeFallback,
    KeyframeSource,
    build_bake_prompt,
)
from .selection import (
    RankedReference,
    SelectionPolicy,
    desired_pose_for,
    pose_affinity,
    rank_references,
    select_best,
    select_top,
)
from .selfcheck import (
    CropEmbedder,
    DriftReport,
    DriftThresholds,
    DriftVerdict,
    IdentitySelfCheck,
    score_descriptor,
)

__all__ = [
    "DEFAULT_PROFILE",
    "KNOWN_PROFILES",
    "BakedKeyframe",
    "CapabilityProfile",
    "ConditionerConfig",
    "ConditioningKind",
    "ConditioningPlan",
    "CropEmbedder",
    "DriftReport",
    "DriftThresholds",
    "DriftVerdict",
    "FallbackConfig",
    "IdentityBundle",
    "IdentityConditioner",
    "IdentitySelfCheck",
    "ImageTransport",
    "KeyframeBaker",
    "KeyframeFallback",
    "KeyframeSource",
    "LockedReference",
    "Pose",
    "RankedReference",
    "SelectionPolicy",
    "bundle_from_canon_slice",
    "build_bake_prompt",
    "centroid",
    "cosine",
    "desired_pose_for",
    "locked_reference_from_ref_image",
    "pose_affinity",
    "profile_for",
    "rank_references",
    "score_descriptor",
    "select_best",
    "select_top",
]
