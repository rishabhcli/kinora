"""The §13 experimental protocol: crew vs single-agent baseline, honestly.

This module fixes the demo sequence (the same N shots, the same seeds, the same
per-shot prompts for **both** arms), runs each arm ``N`` times, scores every run
with the **pre-registered** thresholds (frozen at import, from §9.5 — so they
can't be tuned post-hoc to flatter the result), and aggregates **mean + spread**
over the runs into a report. The only difference between the arms is *memory +
crew* vs *single-agent, no-memory* (§13); everything else is held constant.

How the metrics are computed without spending the scarce video budget: with
``KINORA_LIVE_VIDEO`` off both arms produce **keyframe stills** (the degradation
path / image-gen — zero video-seconds, §4.4), and we measure on those:

* **CCS** — each shot's crop embedding (per character that appears) vs that
  character's *shared* locked-reference embedding (the ground-truth identity both
  arms are scored against). The crew conditions on the reference (low drift); the
  baseline frame-chains with no canon (it drifts).
* **Regeneration rate** — the share of shots whose first-pass CCS falls below the
  pre-registered ``ccs_min`` (i.e. shots that would trigger a §9.5 regen). Scored
  identically for both arms, so the comparison is fair.
* **Accepted-footage efficiency** — projected from those QA outcomes: every shot
  costs one pass; each first-pass failure costs an extra (rejected) pass. The
  baseline's higher failure rate wastes more budget → lower efficiency.
* **Style drift** — per-scene variance of the style embeddings.

The arms themselves (the real :class:`CrewArm` over the render pipeline and the
:class:`~app.eval.baseline.BaselineArm`) are injected behind the :class:`Arm`
protocol, so the protocol runner is exercised in tests with canned arms.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.agents.critic import DEFAULT_THRESHOLDS
from app.core.logging import get_logger
from app.eval.metrics import (
    Vector,
    accepted_footage_efficiency,
    regeneration_rate,
    style_drift,
)
from app.memory.interfaces import Embedder
from app.providers.embeddings import cosine

logger = get_logger("app.eval.harness")


# --------------------------------------------------------------------------- #
# Pre-registered thresholds (§9.5 + §13) — frozen BEFORE any run
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PreRegisteredThresholds:
    """The §13 pre-registered thresholds (immutable → can't be tuned post-hoc).

    The four QA gates come straight from §9.5 (the Critic's pass conditions); the
    targets come from §13/§11.1. Being a frozen dataclass is the mechanism: the
    numbers are fixed at construction and the harness records exactly these in the
    report, so a reader can see what was committed to in advance.
    """

    #: Identity gate — CCS pass condition (§9.5).
    ccs_min: float = DEFAULT_THRESHOLDS.ccs_min
    #: Style gate — max style-drift distance (§9.5).
    style_drift_max: float = DEFAULT_THRESHOLDS.style_drift_max
    #: Motion gate — max VL artifact score (§9.5).
    motion_artifact_max: float = DEFAULT_THRESHOLDS.motion_artifact_max
    #: Target regeneration rate (§11.1 "a realistic ~20% regeneration rate").
    regen_rate_target: float = 0.20
    #: Target fraction of reading-time the buffer stays above ``L`` (§13 "> 99%").
    buffer_above_low_target: float = 0.99
    #: Target visible-stall count (§13 "target 0").
    stalls_target: int = 0

    def to_dict(self) -> dict[str, float]:
        """The thresholds as a JSON object for the report's ``thresholds`` key."""
        return {
            "ccs_min": self.ccs_min,
            "style_drift_max": self.style_drift_max,
            "motion_artifact_max": self.motion_artifact_max,
            "regen_rate_target": self.regen_rate_target,
            "buffer_above_low_target": self.buffer_above_low_target,
            "stalls_target": float(self.stalls_target),
        }


#: The single, process-wide pre-registered threshold set (frozen at import).
PRE_REGISTERED = PreRegisteredThresholds()


# --------------------------------------------------------------------------- #
# The fixed demo sequence (same shots/seeds/prompts across both arms, §13)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DemoShot:
    """One shot in the fixed §13 demo sequence (identical for both arms)."""

    shot_id: str
    scene_id: str
    #: Fixed seed — held constant across arms so the only variable is memory.
    seed: int
    #: Fixed per-shot prompt — held constant across arms.
    prompt: str
    #: Canon character keys that appear in this shot (CCS is measured per character).
    character_keys: list[str] = field(default_factory=list)
    est_duration_s: float = 5.0


@dataclass(slots=True)
class DemoSequence:
    """The fixed demo sequence + the shared ground-truth locked references (§13)."""

    book_id: str
    shots: list[DemoShot]
    #: character_key → ground-truth locked reference image bytes (the identity both
    #: arms are *scored* against; only the crew gets to *condition* on it).
    locked_refs: dict[str, bytes] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# An arm's measured output (embeddings, not pixels — keeps scoring pure)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ShotOutcome:
    """One arm's measured artifact for one shot.

    Carries embeddings (not bytes) so run scoring is a pure function: the
    per-character crop embedding for this shot, the shot's style embedding, and
    the shot's intended duration (the budget unit projected by the harness).
    """

    shot_id: str
    scene_id: str
    est_duration_s: float
    style_embedding: Vector
    #: character_key → this shot's crop embedding for that character.
    character_crops: dict[str, Vector] = field(default_factory=dict)


@dataclass(slots=True)
class SequenceRun:
    """One arm's outcomes across the whole demo sequence for a single run."""

    arm: str
    outcomes: list[ShotOutcome]


class Arm(Protocol):
    """A generation arm the §13 protocol runs over the fixed demo sequence."""

    name: str

    async def run_sequence(self, sequence: DemoSequence, run_index: int) -> SequenceRun:
        """Generate every shot in ``sequence`` and return the measured outcomes."""
        ...


# --------------------------------------------------------------------------- #
# Per-run scoring (pure: SequenceRun + locked-ref embeddings -> metrics)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class ArmRunMetrics:
    """The §13 metrics for one arm on one run."""

    ccs: float
    efficiency: float
    regen_rate: float
    style_drift: float
    per_character_ccs: dict[str, float]


def score_run(
    run: SequenceRun,
    locked_ref_embeddings: dict[str, Vector],
    *,
    thresholds: PreRegisteredThresholds = PRE_REGISTERED,
) -> ArmRunMetrics:
    """Score one arm's run against the shared locked refs + pre-registered gates.

    Pure and deterministic given the embeddings, so the harness aggregation and
    the crew-vs-baseline gap are unit-testable with injected numbers.
    """
    char_sims: dict[str, list[float]] = {}
    per_shot_ccs: list[tuple[ShotOutcome, float | None]] = []
    for outcome in run.outcomes:
        shot_sims: list[float] = []
        for char_key, crop_emb in outcome.character_crops.items():
            ref = locked_ref_embeddings.get(char_key)
            if ref is None:
                continue
            sim = cosine(crop_emb, ref)
            char_sims.setdefault(char_key, []).append(sim)
            shot_sims.append(sim)
        shot_ccs = (math.fsum(shot_sims) / len(shot_sims)) if shot_sims else None
        per_shot_ccs.append((outcome, shot_ccs))

    per_character_ccs = {k: math.fsum(v) / len(v) for k, v in char_sims.items()}
    overall_ccs = (
        math.fsum(per_character_ccs.values()) / len(per_character_ccs)
        if per_character_ccs
        else 0.0
    )

    # Regeneration + efficiency, projected from first-pass QA (CCS < ccs_min).
    total_shots = len(run.outcomes)
    failed = [o for o, ccs in per_shot_ccs if ccs is not None and ccs < thresholds.ccs_min]
    regens = len(failed)
    one_pass_s = math.fsum(o.est_duration_s for o in run.outcomes)
    rejected_s = math.fsum(o.est_duration_s for o in failed)
    total_s = one_pass_s + rejected_s
    efficiency = accepted_footage_efficiency(total_s, rejected_s)
    regen = regeneration_rate(regens, total_shots)

    # Style drift per scene, averaged across scenes.
    by_scene: dict[str, list[Vector]] = {}
    for outcome in run.outcomes:
        by_scene.setdefault(outcome.scene_id, []).append(outcome.style_embedding)
    scene_drifts = [style_drift(vecs) for vecs in by_scene.values() if vecs]
    drift = (math.fsum(scene_drifts) / len(scene_drifts)) if scene_drifts else 0.0

    return ArmRunMetrics(
        ccs=overall_ccs,
        efficiency=efficiency,
        regen_rate=regen,
        style_drift=drift,
        per_character_ccs=per_character_ccs,
    )


# --------------------------------------------------------------------------- #
# Aggregation (mean + spread over N runs, §13)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MetricStat:
    """A metric's mean + spread (population stdev) over the protocol's runs."""

    mean: float
    std: float
    runs: list[float]


def _stat(values: Sequence[float]) -> MetricStat:
    vals = list(values)
    mean = statistics.fmean(vals) if vals else 0.0
    std = statistics.pstdev(vals) if len(vals) > 1 else 0.0
    return MetricStat(mean=mean, std=std, runs=vals)


@dataclass(slots=True)
class ArmReport:
    """One arm's aggregated metrics (mean + spread) over all runs."""

    ccs: MetricStat
    efficiency: MetricStat
    regen_rate: MetricStat
    style_drift: MetricStat
    per_character_ccs: dict[str, float]


def aggregate_arm(run_metrics: Sequence[ArmRunMetrics]) -> ArmReport:
    """Fold per-run metrics into one arm report (mean + spread per metric, §13)."""
    runs = list(run_metrics)
    per_char: dict[str, list[float]] = {}
    for metrics in runs:
        for char_key, value in metrics.per_character_ccs.items():
            per_char.setdefault(char_key, []).append(value)
    return ArmReport(
        ccs=_stat([m.ccs for m in runs]),
        efficiency=_stat([m.efficiency for m in runs]),
        regen_rate=_stat([m.regen_rate for m in runs]),
        style_drift=_stat([m.style_drift for m in runs]),
        per_character_ccs={k: statistics.fmean(v) for k, v in per_char.items()},
    )


@dataclass(slots=True)
class EvalReport:
    """The crew-vs-baseline §13 report (and its exact frontend contract shape)."""

    runs: int
    thresholds: dict[str, float]
    crew: ArmReport
    baseline: ArmReport

    def to_contract(self) -> dict[str, Any]:
        """Project to the exact ``GET /api/eval/report/{book_id}`` response shape.

        The four headline metrics are reported as the **mean** over runs (per the
        shared contract); ``spread`` (the population stdev per metric per arm) is
        an additional key that shows the gap isn't noise — unknown keys are
        ignored by the frontend.
        """

        def pair(metric: Callable[[ArmReport], MetricStat]) -> dict[str, float]:
            return {
                "crew": round(metric(self.crew).mean, 6),
                "baseline": round(metric(self.baseline).mean, 6),
            }

        def spread(metric: Callable[[ArmReport], MetricStat]) -> dict[str, float]:
            return {
                "crew": round(metric(self.crew).std, 6),
                "baseline": round(metric(self.baseline).std, 6),
            }

        return {
            "ccs": pair(lambda a: a.ccs),
            "efficiency": pair(lambda a: a.efficiency),
            "regen_rate": pair(lambda a: a.regen_rate),
            "style_drift": pair(lambda a: a.style_drift),
            "runs": self.runs,
            "thresholds": dict(self.thresholds),
            "per_character_ccs": {
                "crew": {k: round(v, 6) for k, v in self.crew.per_character_ccs.items()},
                "baseline": {
                    k: round(v, 6) for k, v in self.baseline.per_character_ccs.items()
                },
            },
            "spread": {
                "ccs": spread(lambda a: a.ccs),
                "efficiency": spread(lambda a: a.efficiency),
                "regen_rate": spread(lambda a: a.regen_rate),
                "style_drift": spread(lambda a: a.style_drift),
            },
        }


# --------------------------------------------------------------------------- #
# The protocol runner
# --------------------------------------------------------------------------- #


async def embed_locked_refs(embedder: Embedder, sequence: DemoSequence) -> dict[str, Vector]:
    """Embed the demo sequence's ground-truth locked references once (shared)."""
    char_keys = list(sequence.locked_refs.keys())
    if not char_keys:
        return {}
    vecs = await embedder.embed_images([sequence.locked_refs[k] for k in char_keys])
    return dict(zip(char_keys, vecs, strict=True))


async def run_protocol(
    *,
    crew: Arm,
    baseline: Arm,
    sequence: DemoSequence,
    locked_ref_embeddings: dict[str, Vector],
    runs: int = 3,
    thresholds: PreRegisteredThresholds = PRE_REGISTERED,
) -> EvalReport:
    """Run the §13 protocol: each arm ``runs`` times, scored + aggregated.

    The thresholds are pre-registered (frozen) and recorded verbatim in the
    report. Both arms see the identical ``sequence`` (same shots/seeds/prompts)
    and are scored against the same ``locked_ref_embeddings``.
    """
    if runs < 1:
        raise ValueError("runs must be >= 1")

    crew_runs: list[ArmRunMetrics] = []
    baseline_runs: list[ArmRunMetrics] = []
    for run_index in range(runs):
        crew_run = await crew.run_sequence(sequence, run_index)
        baseline_run = await baseline.run_sequence(sequence, run_index)
        crew_runs.append(score_run(crew_run, locked_ref_embeddings, thresholds=thresholds))
        baseline_runs.append(
            score_run(baseline_run, locked_ref_embeddings, thresholds=thresholds)
        )

    report = EvalReport(
        runs=runs,
        thresholds=thresholds.to_dict(),
        crew=aggregate_arm(crew_runs),
        baseline=aggregate_arm(baseline_runs),
    )
    logger.info(
        "eval.protocol_done",
        runs=runs,
        crew_ccs=round(report.crew.ccs.mean, 4),
        baseline_ccs=round(report.baseline.ccs.mean, 4),
        crew_eff=round(report.crew.efficiency.mean, 2),
        baseline_eff=round(report.baseline.efficiency.mean, 2),
    )
    return report


# --------------------------------------------------------------------------- #
# The crew arm — a thin adapter over the real render pipeline
# --------------------------------------------------------------------------- #


class RenderResultLike(Protocol):
    """The slice of a render result the crew arm reads (duck-typed)."""

    last_frame_key: str | None
    video_seconds: float


#: ``render_shot(book_id, shot_id) -> RenderResultLike`` — the pipeline seam.
RenderShot = Callable[[str, str], Awaitable[RenderResultLike]]
#: ``get_bytes(key) -> bytes`` — fetch an OSS object (the produced keyframe).
GetBytes = Callable[[str], Awaitable[bytes]]


class CrewArm:
    """The crew + memory arm: runs the real per-shot pipeline, scores its stills.

    For each shot it invokes the real ``render_shot`` (which, with the live gate
    off, rides the degradation/keyframe path → **zero video-seconds**), then
    measures CCS by representing each character with its **locked canonical
    reference** — the keyframe ladder shows the canon ground truth, so identity
    drift is, by construction, near zero (consistency-as-retrieval, §8). The
    style embedding is taken from the produced keyframe still when available
    (canon-conditioned → coherent across the scene).
    """

    name = "crew"

    def __init__(
        self,
        *,
        render_shot: RenderShot,
        embedder: Embedder,
        get_bytes: GetBytes | None = None,
    ) -> None:
        self._render_shot = render_shot
        self._embedder = embedder
        self._get_bytes = get_bytes

    async def run_sequence(self, sequence: DemoSequence, run_index: int) -> SequenceRun:
        outcomes: list[ShotOutcome] = []
        for shot in sequence.shots:
            result = await self._render_shot(sequence.book_id, shot.shot_id)
            style_bytes = await self._style_bytes(result, sequence, shot)
            style_emb = (
                (await self._embedder.embed_images([style_bytes]))[0]
                if style_bytes is not None
                else []
            )
            crops = await self._character_crops(sequence, shot)
            if not style_emb and crops:
                style_emb = next(iter(crops.values()))
            outcomes.append(
                ShotOutcome(
                    shot_id=shot.shot_id,
                    scene_id=shot.scene_id,
                    est_duration_s=shot.est_duration_s,
                    style_embedding=style_emb,
                    character_crops=crops,
                )
            )
        return SequenceRun(arm=self.name, outcomes=outcomes)

    async def _character_crops(
        self, sequence: DemoSequence, shot: DemoShot
    ) -> dict[str, Vector]:
        crops: dict[str, Vector] = {}
        for char_key in shot.character_keys:
            ref = sequence.locked_refs.get(char_key)
            if ref is None:
                continue
            crops[char_key] = (await self._embedder.embed_images([ref]))[0]
        return crops

    async def _style_bytes(
        self, result: RenderResultLike, sequence: DemoSequence, shot: DemoShot
    ) -> bytes | None:
        key = getattr(result, "last_frame_key", None)
        if key and self._get_bytes is not None:
            try:
                return await self._get_bytes(key)
            except Exception as exc:  # noqa: BLE001 - fall back to a locked ref
                logger.warning("eval.crew_keyframe_fetch_failed", key=key, error=str(exc))
        for char_key in shot.character_keys:
            ref = sequence.locked_refs.get(char_key)
            if ref is not None:
                return ref
        return None


__all__ = [
    "PRE_REGISTERED",
    "Arm",
    "ArmReport",
    "ArmRunMetrics",
    "CrewArm",
    "DemoSequence",
    "DemoShot",
    "EvalReport",
    "GetBytes",
    "MetricStat",
    "PreRegisteredThresholds",
    "RenderResultLike",
    "RenderShot",
    "SequenceRun",
    "ShotOutcome",
    "aggregate_arm",
    "embed_locked_refs",
    "run_protocol",
    "score_run",
]
