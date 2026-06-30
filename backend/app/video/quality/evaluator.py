"""The :class:`ClipEvaluator` — fuse frame stats + VL + §9.5 consistency → one score.

This is the harness's keystone: given one clip sample (sampled frames, the shot's
prompt, the locked identity/style references) plus a pluggable
:class:`~app.video.quality.features.FrameFeatureExtractor` and
:class:`~app.video.quality.vl_scorer.VlScorer`, it produces a model-agnostic
:class:`~app.video.quality.scores.QualityScore`.

How each of the six axes is computed:

* **technical_integrity** — from the extractor's defect features (blockiness / blur /
  banding / temporal-flicker), via :meth:`FrameFeatures.technical_integrity`.
* **aesthetic** — the VL scorer's aesthetic judgment (a viewer call).
* **prompt_adherence** — the VL scorer's adherence judgment.
* **identity_consistency** — CCS = cosine(clip embedding, locked appearance embedding)
  mapped to 0..1, reusing the **§9.5 concept** (``app.providers.cosine``) *without
  importing the Critic*. No clip embedding / no locked refs ⇒ the neutral 1.0.
* **style_consistency** — ``1 − style_drift``, where style_drift = cosine *distance*
  of the clip's style vector vs the scene style centroid (the §9.5 style-centroid
  concept). No style evidence ⇒ neutral 1.0.
* **motion_naturalness** — a tent over the extractor's ``motion_amount``: a frozen
  clip (≈0) and a chaotic one (≈1) are both unnatural; a moderate amount peaks at 1,
  then the temporal-flicker badness further discounts it (jerky motion is unnatural).

Flags: ``artifact_flag`` trips when technical integrity is catastrophic *or* the
flicker badness is extreme; ``nsfw_flag`` comes straight from the VL scorer. A flagged
clip's aggregate is flag-capped by :meth:`QualityScore.from_subscores`.

Everything is async only because the VL seam is async; with the fakes it is fully
deterministic and infra-free.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.providers.embeddings import cosine

from .features import FrameFeatureExtractor, FrameFeatures, FrameStatsExtractor, Gray, Rgb
from .scores import QualityScore, QualityWeights, SubScores, clamp01
from .vl_scorer import StaticVlScorer, VlScorer, VlVerdict

#: Below this technical-integrity goodness a clip is artifact-flagged outright.
_ARTIFACT_INTEGRITY_FLOOR = 0.2
#: A flicker badness at/above this artifact-flags the clip regardless of the blend.
_ARTIFACT_FLICKER_CEIL = 0.85
#: The motion amount (0..1) that reads as the *most natural* (tent peak).
_MOTION_IDEAL = 0.4


def _motion_naturalness(features: FrameFeatures) -> float:
    """Tent over ``motion_amount`` peaking at ``_MOTION_IDEAL``, discounted by flicker.

    A static clip and a thrashing one both fall off the tent; jerky motion (high
    temporal flicker) further discounts the result.
    """
    amount = clamp01(features.motion_amount)
    if amount <= _MOTION_IDEAL:
        tent = amount / _MOTION_IDEAL if _MOTION_IDEAL > 0 else 0.0
    else:
        tent = (1.0 - amount) / (1.0 - _MOTION_IDEAL) if _MOTION_IDEAL < 1 else 0.0
    tent = clamp01(tent)
    return round(clamp01(tent * (1.0 - 0.5 * clamp01(features.temporal_flicker))), 6)


def _ccs(
    clip_embedding: Sequence[float] | None, locked_refs: Sequence[Sequence[float]]
) -> float | None:
    """Identity CCS in 0..1 (the §9.5 concept): best cosine vs any locked ref.

    Returns ``None`` (⇒ neutral) when there is no clip embedding or no refs. The
    cosine is mapped from ``[-1, 1]`` to ``[0, 1]`` so an anti-correlated crop scores
    near 0 rather than being clamped flat.
    """
    if clip_embedding is None or not locked_refs:
        return None
    best = max(cosine(list(clip_embedding), list(ref)) for ref in locked_refs)
    return clamp01((best + 1.0) / 2.0)


def _style_consistency(
    clip_style: Sequence[float] | None, style_centroid: Sequence[float] | None
) -> float | None:
    """``1 − style_drift`` in 0..1 (§9.5 style centroid); ``None`` ⇒ neutral.

    style_drift = cosine *distance* (``1 − cos``) of the clip's style vector vs the
    scene style centroid, then clamped — exactly the §9.5 style metric, graded.
    """
    if clip_style is None or style_centroid is None:
        return None
    drift = 1.0 - cosine(list(clip_style), list(style_centroid))
    return clamp01(1.0 - clamp01(drift))


@dataclass(frozen=True, slots=True)
class ClipSample:
    """One clip to score: frames, the shot's prompt, and the §9.5 locked references.

    ``gray`` / ``rgb`` are pre-decoded frame grids (the real extractor decodes; tests
    hand-build). ``frames_raw`` are the encoded bytes handed to the VL seam (tests
    can leave them empty — the fake VL ignores them). The embeddings are optional;
    absent ⇒ the corresponding consistency axis stays neutral.
    """

    clip_id: str
    provider: str = "unknown"
    prompt: str = ""
    gray: list[Gray] = field(default_factory=list)
    rgb: list[Rgb] = field(default_factory=list)
    frames_raw: list[bytes] = field(default_factory=list)
    clip_embedding: list[float] | None = None
    locked_refs: list[list[float]] = field(default_factory=list)
    clip_style: list[float] | None = None
    style_centroid: list[float] | None = None


@dataclass(slots=True)
class ClipEvaluator:
    """Produce a :class:`QualityScore` for a clip from injected feature + VL seams.

    Defaults: the pure :class:`FrameStatsExtractor` and a neutral
    :class:`StaticVlScorer` (so calling it with no VL configured never hits a model
    and yields the neutral aesthetic/adherence). Inject a fake/real VL scorer to
    enable the perception axes.
    """

    extractor: FrameFeatureExtractor = field(default_factory=FrameStatsExtractor)
    vl_scorer: VlScorer = field(
        default_factory=lambda: StaticVlScorer(VlVerdict(aesthetic=0.5, prompt_adherence=0.5))
    )
    weights: QualityWeights = field(default_factory=QualityWeights)

    async def evaluate(self, sample: ClipSample) -> QualityScore:
        features = self.extractor.extract(sample.gray, sample.rgb)
        verdict = await self.vl_scorer.score(sample.frames_raw, sample.prompt)

        technical = features.technical_integrity()
        motion = _motion_naturalness(features)
        ccs = _ccs(sample.clip_embedding, sample.locked_refs)
        style = _style_consistency(sample.clip_style, sample.style_centroid)

        sub = SubScores(
            technical_integrity=technical,
            aesthetic=verdict.aesthetic,
            prompt_adherence=verdict.prompt_adherence,
            identity_consistency=ccs if ccs is not None else 1.0,
            style_consistency=style if style is not None else 1.0,
            motion_naturalness=motion,
        )

        artifact_flag = (
            technical <= _ARTIFACT_INTEGRITY_FLOOR
            or features.temporal_flicker >= _ARTIFACT_FLICKER_CEIL
        )

        detail: dict[str, float] = {
            "blockiness": features.blockiness,
            "blur": features.blur,
            "banding": features.banding,
            "temporal_flicker": features.temporal_flicker,
            "motion_amount": features.motion_amount,
            "brightness": features.brightness,
            "edge_energy": features.edge_energy,
        }
        if ccs is not None:
            detail["ccs"] = round(ccs, 6)
        if style is not None:
            detail["style_consistency_raw"] = round(style, 6)

        return QualityScore.from_subscores(
            sub,
            provider=sample.provider,
            clip_id=sample.clip_id,
            weights=self.weights,
            artifact_flag=artifact_flag,
            nsfw_flag=verdict.nsfw_flag,
            n_frames=features.n_frames,
            detail=detail,
        )
