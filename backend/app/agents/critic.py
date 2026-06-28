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
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict

from app.core.config import Settings, get_settings
from app.memory.interfaces import CanonSlice
from app.providers import Providers, cosine

from .base import BaseAgent
from .contracts import QARecord, RepairAction, Verdict
from .prompts import CRITIC

if TYPE_CHECKING:
    from app.render.qa.aesthetic import AestheticReport
    from app.render.qa.calibration import CriticCalibration
    from app.render.qa.identity import CharacterCrops, IdentityReport
    from app.render.qa.temporal import TemporalReport
    from app.render.reward import RewardAdvice


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
    advice: RewardAdvice | None = None,
) -> tuple[Verdict, RepairAction, float]:
    """Score + route a clip per §9.5. Pure: returns ``(verdict, repair_action, score)``.

    Routing on failure (by which check failed):
      * timeline contradiction -> raise_conflict (or evolve_canon with text support);
      * identity drift (CCS fail, style ok) -> regen_tighten_refs;
      * style drift -> reprompt_style;
      * motion artifact -> regen_new_seed;
      * retries exhausted -> degrade (the §9.5 retry cap → degradation ladder).

    The optional ``advice`` is the learned-reward layer's advisory
    (:class:`app.render.reward.RewardAdvice`). It is **advisory only** and never
    overrides the pre-registered gate — keeping the §13 pre-registration honest:

      * it can never *rescue* a clip the hard gate failed (routing is unchanged);
      * it can never *silently block* a clip the hard gate passed — the verdict
        stays ``PASS``/``ACCEPT``;
      * its sole effect is that a gate-passing clip the learned model rates poorly
        or finds out-of-distribution is surfaced for review on the ``QARecord``
        (handled by :meth:`Critic.score`, which reads the same ``advice``).

    Passing ``advice=None`` (the cold-start default) is byte-identical to the
    pre-learned behaviour.
    """
    passed = (
        ccs >= thresholds.ccs_min
        and style_drift <= thresholds.style_drift_max
        and timeline_ok
        and motion_artifact <= thresholds.motion_artifact_max
    )
    score = _composite_score(ccs, style_drift, timeline_ok, motion_artifact)
    if passed:
        # The hard gate decides PASS; ``advice`` only informs (see Critic.score).
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
    """Scores a clip against the canon and routes the repair (§9.5).

    The base behaviour is the four pre-registered checks + :func:`decide_qa` routing.
    An optional :class:`~app.render.qa.calibration.CriticCalibration` bundle adds the
    learned-reward layer (§9.5/§13): calibrated thresholds (never looser than the
    pre-registered floor), a per-clip learned reward + anomaly flag, and the richer
    per-character / temporal / aesthetic QA axes. With no calibration injected and no
    multi-character / frame inputs, ``score`` is byte-identical to the original.
    """

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        thresholds: QAThresholds = DEFAULT_THRESHOLDS,
        calibration: CriticCalibration | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="critic",
            model=settings.vl_model,
            prompt=CRITIC,
        )
        self._thresholds = thresholds
        self._calibration = calibration

    @property
    def calibration(self) -> CriticCalibration | None:
        """The injected learned-reward bundle (``None`` ⇒ §9.5 defaults only)."""
        return self._calibration

    def with_calibration(self, calibration: CriticCalibration) -> Critic:
        """Return a Critic using ``calibration`` (cheap — shares the providers/model).

        The periodic :class:`~app.render.qa.calibration.CalibrationPass` produces a
        fresh bundle per book; swapping it in adopts the new calibration without a
        re-init, so the live render path can switch calibrations between shots. The
        clone reuses the *already-resolved* model + prompt (it does NOT re-read
        ``Settings``), so it works even when the environment has no API key.
        """
        clone = Critic.__new__(Critic)
        BaseAgent.__init__(
            clone,
            self._providers,
            name=self.name,
            model=self.model,
            prompt=self.prompt,
            skills=self._skills,
        )
        clone._thresholds = self._thresholds
        clone._calibration = calibration
        return clone

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
        character_crops: list[CharacterCrops] | None = None,
    ) -> QARecord:
        """Compute the checks against the clip and return a routed QA record.

        Identity is the per-character vector (weakest-face gate, §9.5/§13) when
        ``character_crops`` is supplied, else the single-crop CCS. Temporal coherence
        (flicker/morph/limb) and aesthetic quality are measured from the clip frames
        as extra axes. When a calibration bundle is present its calibrated thresholds
        drive the gate (never looser than the pre-registered floor) and its learned
        reward + anomaly flag are attached to the record as an advisory.
        """
        # -- identity: per-character vector or the single-crop CCS ----------- #
        identity = await self._identities(character_crops)
        if identity is not None:
            ccs = identity.aggregate_ccs
            per_character_ccs = identity.ccs_map() or None
        else:
            ccs = await self._ccs(character_crop, locked_ref_image)
            per_character_ccs = None

        style_drift = await self._style_drift(clip_frames, scene_style_centroid)
        vision = await self._vision(clip_frames, canon_slice)

        # -- multimodal temporal + aesthetic axes (deterministic, frame-based) - #
        temporal_report = self._temporal(clip_frames)
        aesthetic_report = self._aesthetic(clip_frames)
        # Fuse the deterministic temporal artifact with the VL motion rating: take the
        # worst (a defect either eye sees is a defect), so neither can hide the other.
        motion_artifact = max(vision.motion_artifact, temporal_report.motion_artifact)

        thresholds = self._effective_thresholds()
        verdict, action, score = decide_qa(
            ccs,
            style_drift,
            vision.timeline_ok,
            motion_artifact,
            textual_evolution_supported=textual_evolution_supported,
            retries_exhausted=retries_exhausted,
            thresholds=thresholds,
        )

        advice = self._advise(
            ccs=ccs,
            style_drift=style_drift,
            timeline_ok=vision.timeline_ok,
            motion_artifact=motion_artifact,
            aesthetic=aesthetic_report.aesthetic,
            temporal=temporal_report.temporal,
            verdict=verdict,
        )

        return QARecord(
            shot_id=shot_id,
            ccs=round(ccs, 4),
            style_drift=round(style_drift, 4),
            timeline_ok=vision.timeline_ok,
            contradicting_state_id=vision.contradicting_state_id,
            motion_artifact=round(motion_artifact, 4),
            score=score,
            verdict=verdict,
            reason=vision.reason,
            repair_action=action,
            learned_reward=advice.reward if advice is not None else None,
            flagged_for_review=bool(advice.flagged_for_review) if advice is not None else False,
            anomaly_score=advice.anomaly_score if advice is not None else None,
            per_character_ccs=per_character_ccs,
            temporal=temporal_report.temporal,
            aesthetic=aesthetic_report.aesthetic,
        )

    # -- learned-reward helpers (pure given the injected bundle) ------------- #

    def _effective_thresholds(self) -> QAThresholds:
        """The gate thresholds — calibrated (floored) when a bundle is present."""
        if self._calibration is None or self._calibration.thresholds.pinned:
            return self._thresholds
        cal = self._calibration.thresholds
        return QAThresholds(
            ccs_min=cal.ccs_min,
            style_drift_max=cal.style_drift_max,
            motion_artifact_max=cal.motion_artifact_max,
        )

    def _advise(
        self,
        *,
        ccs: float,
        style_drift: float,
        timeline_ok: bool,
        motion_artifact: float,
        aesthetic: float,
        temporal: float,
        verdict: Verdict,
    ) -> RewardAdvice | None:
        """The learned advisory for this clip, or ``None`` at cold start.

        Only computed for a gate-*passing* clip (a failing clip is already being
        repaired; its low reward is expected and uninformative), so the advisory's
        ``flagged_for_review`` cleanly means "passed the gate but the learned model is
        unhappy / surprised — surface it".
        """
        if self._calibration is None or verdict is not Verdict.PASS:
            return None
        return self._calibration.advise_clip(
            ccs=ccs,
            style_drift=style_drift,
            timeline_ok=timeline_ok,
            motion_artifact=motion_artifact,
            aesthetic=aesthetic,
            temporal=temporal,
        )

    async def _identities(
        self, character_crops: list[CharacterCrops] | None
    ) -> IdentityReport | None:
        """Per-character CCS vector (weakest-face gate) when crops are supplied."""
        if not character_crops:
            return None
        from app.render.qa.identity import verify_identities

        ccs_min = self._effective_thresholds().ccs_min
        return await verify_identities(
            character_crops, embedder=self._providers.embeddings, ccs_min=ccs_min
        )

    def _temporal(self, clip_frames: list[bytes]) -> TemporalReport:
        """Deterministic temporal-coherence report from the clip frames."""
        from app.render.qa.temporal import TemporalReport, frames_to_gray, temporal_coherence

        grids = frames_to_gray(clip_frames)
        if not grids:
            return TemporalReport()
        return temporal_coherence(grids)

    def _aesthetic(self, clip_frames: list[bytes]) -> AestheticReport:
        """Deterministic perceptual-quality report from the clip frames."""
        from app.render.qa.aesthetic import AestheticReport, aesthetic_score, frames_to_rgb

        grids = frames_to_rgb(clip_frames)
        if not grids:
            return AestheticReport()
        return aesthetic_score(grids)

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
