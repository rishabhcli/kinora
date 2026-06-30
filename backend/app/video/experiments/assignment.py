"""Deterministic, sticky assignment of renders to experiment arms.

A render unit (a shot to be generated) is assigned to exactly one variant by a
pure function of ``(experiment, unit)`` — no RNG, no clock, no storage — so the
same book/shot always lands on the same model in every process and every replay.
That stickiness is what lets a reader watch a whole book without the underlying
model flipping out from under them, and it is what makes an experiment
reproducible.

Two independent hash decisions, both salted off the experiment's own ``salt``:

#. **Enrollment** — is this unit inside the current ``traffic_percent`` slice?
   The canary runner grows this from 1% → 100%; because :func:`in_rollout` only
   ever *adds* units as the percentage grows, a unit enrolled at 1% is still
   enrolled at 25% (monotone ramp, never a flip-flop).
#. **Arm** — given enrollment, which variant? A weighted projection of the unit
   onto the arm split.

The enrollment salt and the arm salt are derived separately so a unit that is
"early" in the ramp is not also biased toward a particular arm.

We reuse :mod:`app.flags.hashing` (SHA-256 → basis-point bucket) rather than
re-implement bucketing, so video assignment is provably identical to the rest of
the platform's rollout math.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.flags.hashing import bucket_bp, in_rollout, weighted_index
from app.video.experiments.models import VideoExperiment, VideoVariant


@dataclass(frozen=True, slots=True)
class RenderUnit:
    """The thing being assigned — one shot the Generator is about to render.

    ``book_id`` and ``shot_id`` are the two natural bucketing keys: bucket by
    book to keep a whole reading consistent on one model, or by shot to vary per
    clip. The remaining fields feed targeting eligibility (mode/resolution/
    duration). Anonymous units (no ids at all) still bucket deterministically off
    whatever key is present.
    """

    book_id: str | None = None
    shot_id: str | None = None
    mode: str | None = None
    resolution: str | None = None
    duration_s: float | None = None

    def bucket_key(self, bucket_by: str) -> str:
        """The string that gets hashed for sticky bucketing.

        Falls back across ``shot_id → book_id`` (or vice-versa) when the chosen
        field is absent so assignment is always well-defined; an entirely
        identity-free unit hashes off a stable sentinel (everything lands in the
        same bucket, which is the only honest behavior with no identity).
        """
        primary = self._field(bucket_by)
        if primary:
            return primary
        # Deterministic fallback so a partial unit still buckets stably.
        for fallback in ("book_id", "shot_id"):
            value = self._field(fallback)
            if value:
                return value
        return "__anonymous__"

    def _field(self, name: str) -> str | None:
        if name == "book_id":
            return self.book_id
        if name == "shot_id":
            return self.shot_id
        return None


class AssignmentReason(str):
    """Marker subtype documenting the small set of assignment outcomes."""


#: Why a unit did or did not get an arm.
REASON_ASSIGNED = "assigned"
REASON_NOT_ELIGIBLE = "not_eligible"  # failed targeting
REASON_NOT_ENROLLED = "not_enrolled"  # outside the traffic_percent slice


@dataclass(frozen=True, slots=True)
class VideoAssignment:
    """The result of assigning one render unit to a video experiment.

    Attributes:
        experiment_key: Which experiment decided this.
        variant: The assigned arm, or ``None`` when not enrolled/eligible (in
            which case the caller should fall back to its default model).
        in_experiment: True only when ``variant`` is a real arm to be logged.
        reason: One of the ``REASON_*`` constants.
        bucket: The unit's basis-point arm bucket ``[0, 10000)`` (telemetry/QA).
    """

    experiment_key: str
    variant: VideoVariant | None
    in_experiment: bool
    reason: str
    bucket: int

    @property
    def variant_key(self) -> str | None:
        return self.variant.key if self.variant is not None else None


class VideoAssigner:
    """Assigns render units to arms of one :class:`VideoExperiment`.

    Stateless and pure: construct with an experiment, call :meth:`assign` as many
    times as you like; the same unit always returns the same arm for a given
    experiment definition (the ``traffic_percent`` carried on the experiment is
    what the canary runner ramps).
    """

    def __init__(self, experiment: VideoExperiment) -> None:
        self._exp = experiment

    @property
    def experiment(self) -> VideoExperiment:
        return self._exp

    def _enroll_salt(self) -> str:
        return f"{self._exp.salt}:enroll"

    def _arm_salt(self) -> str:
        return f"{self._exp.salt}:arm"

    def assign(self, unit: RenderUnit) -> VideoAssignment:
        """Deterministically map ``unit`` to an arm (or to no-arm/control)."""
        exp = self._exp
        key = unit.bucket_key(exp.bucket_by)
        bucket = bucket_bp(key, self._arm_salt())

        if not exp.targeting.matches(
            mode=unit.mode,
            book_id=unit.book_id,
            resolution=unit.resolution,
            duration_s=unit.duration_s,
        ):
            return self._miss(REASON_NOT_ELIGIBLE, bucket)

        if not in_rollout(key, self._enroll_salt(), exp.traffic_percent):
            return self._miss(REASON_NOT_ENROLLED, bucket)

        weights = tuple(v.weight for v in exp.variants)
        index = weighted_index(key, self._arm_salt(), weights)
        variant = exp.variants[index]
        return VideoAssignment(
            experiment_key=exp.key,
            variant=variant,
            in_experiment=True,
            reason=REASON_ASSIGNED,
            bucket=bucket,
        )

    def _miss(self, reason: str, bucket: int) -> VideoAssignment:
        return VideoAssignment(
            experiment_key=self._exp.key,
            variant=None,
            in_experiment=False,
            reason=reason,
            bucket=bucket,
        )


__all__ = [
    "REASON_ASSIGNED",
    "REASON_NOT_ELIGIBLE",
    "REASON_NOT_ENROLLED",
    "RenderUnit",
    "VideoAssigner",
    "VideoAssignment",
]
