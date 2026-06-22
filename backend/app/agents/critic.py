"""Critic / QA — the self-correcting loop with concrete thresholds (§9.5, §10).

The Critic (``qwen-vl-max``) scores each clip against the canon slice on four
checks with hard, pre-registered thresholds:

==========  ========================================================  =========
Check       Metric                                                    Pass
==========  ========================================================  =========
Identity    CCS = cosine(crop embedding, locked appearance embedding) >= 0.85
Style       cosine distance of clip style vs scene style centroid     <= 0.08
Timeline    VL boolean: no depicted fact contradicts an active state  true
Motion      VL 0..1 artifact rating (flicker / morph / extra limbs)   <= 0.25
==========  ========================================================  =========

A verdict is ``pass`` iff all four hold. The repair routing (which fix for which
failure) is :func:`decide_qa` — a pure function of the four numbers (plus the
retry-cap and a legitimate-evolution flag), so the thresholds and routing are
unit-testable deterministically by injecting the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.memory.interfaces import CanonSlice
from app.providers import Providers, cosine

from .base import BaseAgent
from .contracts import QARecord, RepairAction, Verdict
from .prompts import CRITIC


@dataclass(frozen=True, slots=True)
class QAThresholds:
    """The §9.5 pass thresholds (pre-registered; do not tune to flatter results)."""

    ccs_min: float = 0.85
    style_drift_max: float = 0.08
    motion_artifact_max: float = 0.25


DEFAULT_THRESHOLDS = QAThresholds()


class CriticVision(BaseModel):
    """The VL model's judgments — the two calls only a viewer can make (internal)."""

    model_config = ConfigDict(extra="ignore")

    timeline_ok: bool = True
    contradicting_state_id: str | None = None
    motion_artifact: float = 0.0
    reason: str = ""


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _composite_score(
    ccs: float, style_drift: float, timeline_ok: bool, motion_artifact: float
) -> float:
    """A 0..1 overall score (mean of the four normalized sub-scores)."""
    parts = [
        _clamp01(ccs),
        _clamp01(1.0 - style_drift),
        1.0 if timeline_ok else 0.0,
        _clamp01(1.0 - motion_artifact),
    ]
    return round(sum(parts) / len(parts), 4)


def decide_qa(
    ccs: float,
    style_drift: float,
    timeline_ok: bool,
    motion_artifact: float,
    *,
    textual_evolution_supported: bool = False,
    retries_exhausted: bool = False,
    thresholds: QAThresholds = DEFAULT_THRESHOLDS,
) -> tuple[Verdict, RepairAction, float]:
    """Score + route a clip per §9.5. Pure: returns ``(verdict, repair_action, score)``.

    Routing on failure (by which check failed):
      * timeline contradiction -> raise_conflict (or evolve_canon with text support);
      * identity drift (CCS fail, style ok) -> regen_tighten_refs;
      * style drift -> reprompt_style;
      * motion artifact -> regen_new_seed;
      * retries exhausted -> degrade (the §9.5 retry cap → degradation ladder).
    """
    passed = (
        ccs >= thresholds.ccs_min
        and style_drift <= thresholds.style_drift_max
        and timeline_ok
        and motion_artifact <= thresholds.motion_artifact_max
    )
    score = _composite_score(ccs, style_drift, timeline_ok, motion_artifact)
    if passed:
        return Verdict.PASS, RepairAction.ACCEPT, score
    if retries_exhausted:
        return Verdict.FAIL, RepairAction.DEGRADE, score
    if not timeline_ok:
        action = (
            RepairAction.EVOLVE_CANON
            if textual_evolution_supported
            else RepairAction.RAISE_CONFLICT
        )
    elif ccs < thresholds.ccs_min and style_drift <= thresholds.style_drift_max:
        action = RepairAction.REGEN_TIGHTEN_REFS
    elif style_drift > thresholds.style_drift_max:
        action = RepairAction.REPROMPT_STYLE
    else:  # motion_artifact > max (the only remaining failing check)
        action = RepairAction.REGEN_NEW_SEED
    return Verdict.FAIL, action, score


class Critic(BaseAgent):
    """Scores a clip against the canon and routes the repair (§9.5)."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        thresholds: QAThresholds = DEFAULT_THRESHOLDS,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="critic",
            model=settings.vl_model,
            prompt=CRITIC,
        )
        self._thresholds = thresholds

    async def score(
        self,
        *,
        shot_id: str,
        clip_frames: list[bytes],
        canon_slice: CanonSlice,
        character_crop: bytes | None = None,
        locked_ref_image: bytes | None = None,
        scene_style_centroid: list[float] | None = None,
        textual_evolution_supported: bool = False,
        retries_exhausted: bool = False,
    ) -> QARecord:
        """Compute the four checks against the clip and return a routed QA record."""
        ccs = await self._ccs(character_crop, locked_ref_image)
        style_drift = await self._style_drift(clip_frames, scene_style_centroid)
        vision = await self._vision(clip_frames, canon_slice)
        verdict, action, score = decide_qa(
            ccs,
            style_drift,
            vision.timeline_ok,
            vision.motion_artifact,
            textual_evolution_supported=textual_evolution_supported,
            retries_exhausted=retries_exhausted,
            thresholds=self._thresholds,
        )
        return QARecord(
            shot_id=shot_id,
            ccs=round(ccs, 4),
            style_drift=round(style_drift, 4),
            timeline_ok=vision.timeline_ok,
            contradicting_state_id=vision.contradicting_state_id,
            motion_artifact=round(vision.motion_artifact, 4),
            score=score,
            verdict=verdict,
            reason=vision.reason,
            repair_action=action,
        )

    # -- the four checks ----------------------------------------------------- #

    async def _ccs(self, crop: bytes | None, locked_ref: bytes | None) -> float:
        """CCS = cosine(crop embedding, locked appearance embedding) (§9.5)."""
        if crop is None or locked_ref is None:
            return 1.0  # no locked character to verify → identity check is N/A
        crop_vec = (await self._providers.embeddings.embed_images([crop]))[0]
        ref_vec = (await self._providers.embeddings.embed_images([locked_ref]))[0]
        return cosine(crop_vec, ref_vec)

    async def _style_drift(
        self, clip_frames: list[bytes], centroid: list[float] | None
    ) -> float:
        """Cosine distance of the clip's style embedding from the scene centroid."""
        if not clip_frames or not centroid:
            return 0.0  # no centroid to compare against → no measurable drift
        vectors = await self._providers.embeddings.embed_images(clip_frames)
        clip_style = _mean_vector(vectors)
        return max(0.0, 1.0 - cosine(clip_style, centroid))

    async def _vision(self, clip_frames: list[bytes], canon_slice: CanonSlice) -> CriticVision:
        """VL pass for the timeline + motion judgments (identity/style are numeric)."""
        if not clip_frames:
            return CriticVision(timeline_ok=True, motion_artifact=0.0, reason="no frames to score")
        payload = {
            "instruction": "Judge timeline_ok and motion_artifact for the frames.",
            "active_states": [s.model_dump(mode="json") for s in canon_slice.active_states],
        }
        frames: list[bytes | str] = list(clip_frames)
        return await self.run_json_vl(frames, payload, CriticVision, temperature=0.0)


def _mean_vector(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    length = len(vectors[0])
    sums = [0.0] * length
    for vec in vectors:
        for i in range(length):
            sums[i] += vec[i]
    count = float(len(vectors))
    return [s / count for s in sums]


__all__ = [
    "DEFAULT_THRESHOLDS",
    "Critic",
    "CriticVision",
    "QAThresholds",
    "decide_qa",
]
