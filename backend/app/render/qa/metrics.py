"""The §13 eval harness — the metrics that prove "consistency is a memory problem".

§13 demands "a measurable efficiency gain over single-agent baselines": run the crew
(memory + the §9.5 critic loop) and a single-agent baseline (no memory, no critic)
over the *same* demo book and chart the headline numbers side by side. This module is
the **pure metric layer** the harness sits on — it computes each §13 metric from a set
of already-scored shot outcomes, with no model call and no I/O, so the chart is built
from the QA records the Critic already produces.

Metrics (verbatim from §13):

* **CCS** — mean appearance-embedding cosine for a character across every shot they
  appear in (``CCS = mean_i cos(emb(crop_i), emb(locked_ref))``). Higher is better;
  computed per-character and aggregated.
* **Accepted-footage efficiency** — seconds of QA-passed video per 100s of budget
  (``efficiency = (1 − rejected_seconds / total_seconds) × 100``). The headline.
* **Regeneration rate** — ``regens / total_shots``. Lower is better.
* **Style drift** — variance of per-shot style-drift across a scene (``var_i``). Lower
  = a more coherent look.

§13's experimental protocol stresses **honest pre-registration**: fix seeds + prompts
across both arms, vary only memory+crew, report mean and spread across runs. This
module supports that by computing the metrics and an :class:`ArmComparison` of two
arms with the relative gain — the one slide §13 asks for.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from statistics import fmean, pvariance


@dataclass(frozen=True, slots=True)
class ShotOutcome:
    """One scored shot as the metrics harness sees it (a QARecord projection).

    Maps 1:1 onto the fields the Critic already produces, so a list of
    :class:`~app.agents.contracts.QARecord` (plus a duration + accept flag) projects
    directly into this without importing the agents layer.
    """

    shot_id: str
    accepted: bool
    duration_s: float
    ccs: float
    style_drift: float
    regenerations: int = 0
    character_key: str | None = None


@dataclass(frozen=True, slots=True)
class CharacterCCS:
    """Per-character CCS — §13's ``mean_i cos(crop_i, locked_ref)`` for one character."""

    character_key: str
    mean_ccs: float
    n_shots: int


@dataclass(frozen=True, slots=True)
class ArmMetrics:
    """All §13 metrics for one experimental arm (crew, or single-agent baseline)."""

    arm: str
    n_shots: int = 0
    mean_ccs: float = 1.0
    per_character_ccs: list[CharacterCCS] = field(default_factory=list)
    accepted_footage_efficiency: float = 100.0
    regeneration_rate: float = 0.0
    style_drift_variance: float = 0.0
    accepted_seconds: float = 0.0
    total_seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class ArmComparison:
    """The §13 headline: crew vs. baseline on the two metrics that matter."""

    crew: ArmMetrics
    baseline: ArmMetrics
    #: How much higher the crew's CCS is, in absolute cosine points.
    ccs_gain: float = 0.0
    #: Relative accepted-footage-efficiency gain of the crew over the baseline (%).
    efficiency_gain_pct: float = 0.0
    #: How much lower the crew's regeneration rate is (absolute).
    regen_reduction: float = 0.0
    #: How much lower the crew's style-drift variance is (absolute).
    style_drift_reduction: float = 0.0


def character_ccs(outcomes: Iterable[ShotOutcome]) -> list[CharacterCCS]:
    """Per-character mean CCS over every shot a character appears in (§13)."""
    by_char: dict[str, list[float]] = {}
    for o in outcomes:
        if o.character_key is None:
            continue
        by_char.setdefault(o.character_key, []).append(o.ccs)
    return [
        CharacterCCS(character_key=k, mean_ccs=round(fmean(v), 4), n_shots=len(v))
        for k, v in sorted(by_char.items())
    ]


def accepted_footage_efficiency(outcomes: Sequence[ShotOutcome]) -> tuple[float, float, float]:
    """§13 ``(1 − rejected_seconds / total_seconds) × 100`` + the raw seconds.

    Returns ``(efficiency_pct, accepted_seconds, total_seconds)``. *Total* seconds is
    everything generated (accepted + rejected); rejected footage is the budget burned
    on clips the QA loop threw away — the crew should burn less because memory gets
    each shot right the first time.
    """
    total = sum(o.duration_s for o in outcomes)
    accepted = sum(o.duration_s for o in outcomes if o.accepted)
    if total <= 0:
        return 100.0, 0.0, 0.0
    efficiency = (accepted / total) * 100.0
    return round(efficiency, 4), round(accepted, 4), round(total, 4)


def regeneration_rate(outcomes: Sequence[ShotOutcome]) -> float:
    """§13 ``regens / total_shots`` — lower is better (memory ⇒ right first time)."""
    if not outcomes:
        return 0.0
    regens = sum(o.regenerations for o in outcomes)
    return round(regens / len(outcomes), 4)


def style_drift_variance(outcomes: Sequence[ShotOutcome]) -> float:
    """§13 ``var_i(style_drift_i)`` — lower = a more coherent look across the scene."""
    drifts = [o.style_drift for o in outcomes]
    if len(drifts) < 2:
        return 0.0
    return round(pvariance(drifts), 6)


def arm_metrics(arm: str, outcomes: Sequence[ShotOutcome]) -> ArmMetrics:
    """Compute every §13 metric for one experimental arm (pure)."""
    per_char = character_ccs(outcomes)
    all_ccs = [o.ccs for o in outcomes]
    mean_ccs = round(fmean(all_ccs), 4) if all_ccs else 1.0
    efficiency, accepted_s, total_s = accepted_footage_efficiency(outcomes)
    return ArmMetrics(
        arm=arm,
        n_shots=len(outcomes),
        mean_ccs=mean_ccs,
        per_character_ccs=per_char,
        accepted_footage_efficiency=efficiency,
        regeneration_rate=regeneration_rate(outcomes),
        style_drift_variance=style_drift_variance(outcomes),
        accepted_seconds=accepted_s,
        total_seconds=total_s,
    )


def compare_arms(crew: ArmMetrics, baseline: ArmMetrics) -> ArmComparison:
    """Build the §13 crew-vs-baseline headline comparison (pure)."""
    base_eff = baseline.accepted_footage_efficiency or 1e-9
    efficiency_gain = (
        (crew.accepted_footage_efficiency - baseline.accepted_footage_efficiency)
        / base_eff
        * 100.0
    )
    return ArmComparison(
        crew=crew,
        baseline=baseline,
        ccs_gain=round(crew.mean_ccs - baseline.mean_ccs, 4),
        efficiency_gain_pct=round(efficiency_gain, 4),
        regen_reduction=round(baseline.regeneration_rate - crew.regeneration_rate, 4),
        style_drift_reduction=round(
            baseline.style_drift_variance - crew.style_drift_variance, 6
        ),
    )


@dataclass(frozen=True, slots=True)
class RunStats:
    """Mean ± spread of a metric across N protocol runs (§13 "spread isn't noise")."""

    mean: float
    stdev: float
    n: int


def aggregate_runs(values: Sequence[float]) -> RunStats:
    """Mean + population stdev of a metric across runs (§13's 3-run mean ± spread)."""
    if not values:
        return RunStats(mean=0.0, stdev=0.0, n=0)
    mu = fmean(values)
    var = pvariance(values) if len(values) > 1 else 0.0
    return RunStats(mean=round(mu, 4), stdev=round(var**0.5, 4), n=len(values))


__all__ = [
    "ArmComparison",
    "ArmMetrics",
    "CharacterCCS",
    "RunStats",
    "ShotOutcome",
    "accepted_footage_efficiency",
    "aggregate_runs",
    "arm_metrics",
    "character_ccs",
    "compare_arms",
    "regeneration_rate",
    "style_drift_variance",
]
