"""Reference-image selection / ranking — pick the best locked ref for a shot.

A shot has a *framing intent* (the Cinematographer's camera: a ``closeup`` push-in
vs a ``wide`` establishing, §7.1). Conditioning the render on the locked ref whose
pose matches that framing preserves identity far better than always handing over
the same front shot — a profile shot driven from a front ref drifts. This module
is a pure, deterministic ranking policy:

* score each locked ref by **pose affinity** to the shot's desired pose (a small
  affinity matrix — front↔3q are close, front↔profile less so), plus the ref's
  intrinsic :attr:`~app.video.identity.bundle.LockedReference.quality`;
* rank, then select either the single best (for first-frame providers) or the top
  ``k`` (for a reference-set provider, capped by its capability profile).

No RNG, no clock — same inputs, same order, every time.
"""

from __future__ import annotations

from dataclasses import dataclass

from .bundle import IdentityBundle, LockedReference, Pose

# --------------------------------------------------------------------------- #
# Pose affinity
# --------------------------------------------------------------------------- #

#: Symmetric affinity between a *desired* pose and a *candidate* ref pose in
#: ``[0,1]``. 1.0 = exact, lower = a worse stand-in. Unlisted pairs fall back to
#: :data:`_DEFAULT_AFFINITY`. ``UNKNOWN`` is a neutral mid affinity to everything
#: (an unannotated ref is a usable but not preferred stand-in).
_AFFINITY: dict[tuple[Pose, Pose], float] = {
    (Pose.FRONT, Pose.THREE_QUARTER): 0.85,
    (Pose.FRONT, Pose.PROFILE): 0.55,
    (Pose.FRONT, Pose.CLOSEUP): 0.9,
    (Pose.FRONT, Pose.FULL_BODY): 0.75,
    (Pose.THREE_QUARTER, Pose.PROFILE): 0.8,
    (Pose.THREE_QUARTER, Pose.CLOSEUP): 0.8,
    (Pose.PROFILE, Pose.BACK): 0.45,
    (Pose.CLOSEUP, Pose.FULL_BODY): 0.6,
    (Pose.ESTABLISHING, Pose.FULL_BODY): 0.7,
}

_DEFAULT_AFFINITY = 0.4
_UNKNOWN_AFFINITY = 0.5
_EXACT_AFFINITY = 1.0


def pose_affinity(desired: Pose, candidate: Pose) -> float:
    """Affinity in ``[0,1]`` of using ``candidate`` to satisfy ``desired`` (pure)."""
    if desired is candidate:
        return _EXACT_AFFINITY
    if desired is Pose.UNKNOWN or candidate is Pose.UNKNOWN:
        return _UNKNOWN_AFFINITY
    return _AFFINITY.get((desired, candidate)) or _AFFINITY.get(
        (candidate, desired), _DEFAULT_AFFINITY
    )


# --------------------------------------------------------------------------- #
# Shot framing → desired pose
# --------------------------------------------------------------------------- #

#: Map a Cinematographer ``shot_size`` token (§7.1 camera) to the pose that best
#: conditions it. Loose by design — the affinity matrix handles the rest.
_SHOT_SIZE_TO_POSE: dict[str, Pose] = {
    "closeup": Pose.CLOSEUP,
    "close": Pose.CLOSEUP,
    "cu": Pose.CLOSEUP,
    "ecu": Pose.CLOSEUP,
    "medium": Pose.THREE_QUARTER,
    "med": Pose.THREE_QUARTER,
    "ms": Pose.THREE_QUARTER,
    "wide": Pose.FULL_BODY,
    "full": Pose.FULL_BODY,
    "long": Pose.FULL_BODY,
    "ls": Pose.FULL_BODY,
    "establishing": Pose.ESTABLISHING,
    "ws": Pose.ESTABLISHING,
}


def desired_pose_for(shot_size: str | None) -> Pose:
    """The preferred ref pose for a shot's framing (``FRONT`` when unspecified)."""
    if not shot_size:
        return Pose.FRONT
    return _SHOT_SIZE_TO_POSE.get(shot_size.strip().lower(), Pose.FRONT)


# --------------------------------------------------------------------------- #
# Ranking
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RankedReference:
    """A locked ref with its computed selection score (for telemetry/debugging)."""

    reference: LockedReference
    score: float
    affinity: float


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Weights for reference ranking (deterministic; no env reads).

    Attributes:
        pose_weight: How strongly pose affinity drives the score.
        quality_weight: How strongly intrinsic ref quality drives the score.
        prefer_descriptor: When True, refs that carry an appearance descriptor get
            a small bonus (they let the self-check verify the chosen ref).
        descriptor_bonus: The size of that bonus.
    """

    pose_weight: float = 0.7
    quality_weight: float = 0.3
    prefer_descriptor: bool = True
    descriptor_bonus: float = 0.05


def _score(ref: LockedReference, desired: Pose, policy: SelectionPolicy) -> RankedReference:
    aff = pose_affinity(desired, ref.pose)
    score = policy.pose_weight * aff + policy.quality_weight * ref.quality
    if policy.prefer_descriptor and ref.has_descriptor:
        score += policy.descriptor_bonus
    return RankedReference(reference=ref, score=score, affinity=aff)


def rank_references(
    bundle: IdentityBundle,
    *,
    desired_pose: Pose = Pose.FRONT,
    policy: SelectionPolicy | None = None,
    require_bytes: bool = False,
) -> list[RankedReference]:
    """Rank a bundle's locked refs best-first for ``desired_pose`` (pure, stable).

    Ties (equal score) preserve the bundle's input order via a stable sort, so the
    same bundle + pose always yields the same ranking. ``require_bytes`` filters to
    refs that carry pixel bytes (needed for inline-base64 transports / keyframe
    baking).
    """
    pol = policy or SelectionPolicy()
    refs = [
        r
        for r in bundle.locked_references
        if not require_bytes or r.has_bytes
    ]
    scored = [_score(r, desired_pose, pol) for r in refs]
    # Stable descending sort: negate score so Python's stable ascending sort keeps
    # input order among equals.
    return sorted(scored, key=lambda rr: -rr.score)


def select_best(
    bundle: IdentityBundle,
    *,
    desired_pose: Pose = Pose.FRONT,
    policy: SelectionPolicy | None = None,
    require_bytes: bool = False,
) -> LockedReference | None:
    """The single best locked ref for ``desired_pose`` (``None`` when none fit)."""
    ranked = rank_references(
        bundle, desired_pose=desired_pose, policy=policy, require_bytes=require_bytes
    )
    return ranked[0].reference if ranked else None


def select_top(
    bundle: IdentityBundle,
    *,
    k: int,
    desired_pose: Pose = Pose.FRONT,
    policy: SelectionPolicy | None = None,
    require_bytes: bool = False,
) -> list[LockedReference]:
    """The top-``k`` locked refs for ``desired_pose`` (best first; ``k<=0`` → []).

    For a reference-set provider: hand over several locked poses (capped by the
    backend's ``max_reference_images``), best-matching first.
    """
    if k <= 0:
        return []
    ranked = rank_references(
        bundle, desired_pose=desired_pose, policy=policy, require_bytes=require_bytes
    )
    return [rr.reference for rr in ranked[:k]]


__all__ = [
    "RankedReference",
    "SelectionPolicy",
    "desired_pose_for",
    "pose_affinity",
    "rank_references",
    "select_best",
    "select_top",
]
