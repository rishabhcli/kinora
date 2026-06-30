"""The model-agnostic quality ruler — :class:`QualityScore` and its sub-scores.

Different video providers (Wan turbo/plus, MiniMax/Hailuo, a local TI2V tester …)
vary wildly in output quality, and Kinora needs **one ruler** to score *any* model's
clip so the router/registry can pick, route, and rank providers. This module defines
that ruler as a frozen, normalized, model-agnostic score with six axes:

==================  ====================================================  =======
Axis                What it measures                                      Range
==================  ====================================================  =======
technical_integrity blockiness / blur / banding / temporal-flicker        0..1 ↑
aesthetic           perceptual "good-looking-ness" (no-reference IQA)      0..1 ↑
prompt_adherence    does the clip depict the shot's spec / prompt          0..1 ↑
identity_consistency CCS vs the locked appearance refs (§9.5 Identity)     0..1 ↑
style_consistency   1 − style-drift vs the scene style centroid (§9.5)     0..1 ↑
motion_naturalness  motion is present *and* artifact-free (§9.5 Motion)    0..1 ↑

All axes are *goodness* in ``0..1`` (higher = better), so the aggregate is a plain
weighted mean — a single comparable number per clip regardless of which model made it.
This deliberately mirrors §9.5's CCS / style-centroid / motion concepts **without
importing or mutating the Critic**: the Critic is a hard *pass/fail gate* for the
live render loop; this is a *graded reputation ruler* for provider selection. They
share vocabulary, not code.

Two flags ride alongside the score and never enter the weighted mean (a beautiful
clip that is NSFW or riddled with artifacts must not be "good"): ``artifact_flag``
and ``nsfw_flag``. A flagged clip is reported with ``flagged=True`` and its aggregate
is *capped* (not zeroed) so the ledger still sees graceful degradation rather than a
cliff.

Everything here is pure + deterministic; the frame statistics and VL judgments are
injected (see ``features.py`` / ``vl_scorer.py``), so the math unit-tests on hand-
built numbers with no infra.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator


def clamp01(value: float) -> float:
    """Clamp to the closed unit interval (NaN → 0.0)."""
    if math.isnan(value):
        return 0.0
    return max(0.0, min(1.0, value))


class SubScores(BaseModel):
    """The six model-agnostic quality axes (each 0..1 *goodness*, higher = better).

    Mirrors the §9.5 vocabulary (CCS, style centroid, motion) as graded axes rather
    than the Critic's hard gate. Defaults are the neutral "no evidence" value 1.0 for
    consistency axes (a clip with no locked refs is not *penalised* for identity) and
    a conservative 0.5 for the perception axes that always have frame evidence.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    technical_integrity: float = Field(default=0.5, ge=0.0, le=1.0)
    aesthetic: float = Field(default=0.5, ge=0.0, le=1.0)
    prompt_adherence: float = Field(default=0.5, ge=0.0, le=1.0)
    identity_consistency: float = Field(default=1.0, ge=0.0, le=1.0)
    style_consistency: float = Field(default=1.0, ge=0.0, le=1.0)
    motion_naturalness: float = Field(default=0.5, ge=0.0, le=1.0)

    def as_mapping(self) -> dict[str, float]:
        """The six axes as a plain ``name -> value`` dict (stable key order)."""
        return {
            "technical_integrity": self.technical_integrity,
            "aesthetic": self.aesthetic,
            "prompt_adherence": self.prompt_adherence,
            "identity_consistency": self.identity_consistency,
            "style_consistency": self.style_consistency,
            "motion_naturalness": self.motion_naturalness,
        }


class QualityWeights(BaseModel):
    """Blend weights for the six axes (need not sum to 1 — the mean re-normalizes).

    The defaults weight *consistency* (identity + style, the §9.5 thesis that a long
    adaptation stays coherent) and *technical integrity* above raw prettiness, so a
    gorgeous-but-drifting clip ranks below a plainer-but-faithful one. Tune per book
    or per use-case without touching the math.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    technical_integrity: float = Field(default=0.22, ge=0.0)
    aesthetic: float = Field(default=0.13, ge=0.0)
    prompt_adherence: float = Field(default=0.18, ge=0.0)
    identity_consistency: float = Field(default=0.22, ge=0.0)
    style_consistency: float = Field(default=0.15, ge=0.0)
    motion_naturalness: float = Field(default=0.10, ge=0.0)

    @field_validator("*")
    @classmethod
    def _finite(cls, v: float) -> float:
        if math.isnan(v) or math.isinf(v):
            raise ValueError("weights must be finite")
        return v

    def as_mapping(self) -> dict[str, float]:
        return {
            "technical_integrity": self.technical_integrity,
            "aesthetic": self.aesthetic,
            "prompt_adherence": self.prompt_adherence,
            "identity_consistency": self.identity_consistency,
            "style_consistency": self.style_consistency,
            "motion_naturalness": self.motion_naturalness,
        }


DEFAULT_WEIGHTS = QualityWeights()

#: When a clip is flagged (artifact or NSFW), its aggregate is capped here rather than
#: zeroed — the ledger sees a steep but graceful penalty, not a discontinuity.
FLAGGED_AGGREGATE_CAP = 0.25


def weighted_aggregate(sub: SubScores, weights: QualityWeights = DEFAULT_WEIGHTS) -> float:
    """Weighted mean of the six axes in ``0..1`` (re-normalized by the weight sum).

    A zero (or all-zero) weight vector falls back to a plain unweighted mean so the
    function never divides by zero and a misconfigured weight set still yields a
    sensible number.
    """
    s = sub.as_mapping()
    w = weights.as_mapping()
    wsum = math.fsum(w.values())
    if wsum <= 0.0:
        return round(math.fsum(s.values()) / len(s), 6)
    num = math.fsum(w[k] * s[k] for k in s)
    return round(clamp01(num / wsum), 6)


class QualityScore(BaseModel):
    """The model-agnostic per-clip quality verdict — one number on one ruler.

    Carries the six sub-scores, the weighted ``aggregate`` (already flag-capped), the
    two safety/artifact flags, and free-form ``detail`` provenance (which extractor /
    VL scorer produced what) for auditability. Construct via :meth:`from_subscores`
    so the aggregate + ``flagged`` are always consistent with the inputs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = "unknown"
    clip_id: str = ""
    sub_scores: SubScores
    aggregate: float = Field(ge=0.0, le=1.0)
    artifact_flag: bool = False
    nsfw_flag: bool = False
    n_frames: int = 0
    detail: Mapping[str, float] = Field(default_factory=dict)

    @property
    def flagged(self) -> bool:
        """True iff either safety/artifact flag is set (aggregate is capped)."""
        return self.artifact_flag or self.nsfw_flag

    @classmethod
    def from_subscores(
        cls,
        sub: SubScores,
        *,
        provider: str = "unknown",
        clip_id: str = "",
        weights: QualityWeights = DEFAULT_WEIGHTS,
        artifact_flag: bool = False,
        nsfw_flag: bool = False,
        n_frames: int = 0,
        detail: Mapping[str, float] | None = None,
    ) -> QualityScore:
        """Build a score, computing + flag-capping the aggregate from the sub-scores."""
        agg = weighted_aggregate(sub, weights)
        if artifact_flag or nsfw_flag:
            agg = min(agg, FLAGGED_AGGREGATE_CAP)
        return cls(
            provider=provider,
            clip_id=clip_id,
            sub_scores=sub,
            aggregate=round(agg, 6),
            artifact_flag=artifact_flag,
            nsfw_flag=nsfw_flag,
            n_frames=n_frames,
            detail=dict(detail or {}),
        )
