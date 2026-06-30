"""Paired analysis over a :class:`ComparisonDataset` — the candidate's report card.

Turns the accumulated paired samples into the numbers a promotion decision needs:

* **Quality** — paired mean delta + t-CI + bootstrap-CI, the candidate win-rate
  (with a practically-significant dead-band), and a distribution-free Wilcoxon
  signed-rank check. All over the *comparable* subset (both models succeeded and
  were scored).
* **Cost** — mean signed video-second delta per shot (negative = cheaper) over
  both-succeeded shots, plus the total eval video-seconds the candidate burned.
* **Latency** — mean signed latency delta (ms) per shot.
* **Reliability** — each model's non-gated failure rate.

Pure: a function of the dataset + thresholds, no I/O. The result is a frozen
pydantic model so it serialises straight into the recommendation report / an API.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict

from . import stats
from .collector import (
    ComparisonDataset,
    FailureTally,
    candidate_failures,
    production_failures,
)


class QualityAnalysis(BaseModel):
    """Paired quality comparison of candidate vs production."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n_comparable: int
    mean_delta: float
    t_ci_low: float
    t_ci_high: float
    t_p_value: float
    bootstrap_ci_low: float
    bootstrap_ci_high: float
    win_rate: float
    win_ci_low: float
    win_ci_high: float
    wins: int
    losses: int
    ties: int
    wilcoxon_p_value: float
    confidence: float

    @property
    def quality_ci_excludes_zero(self) -> bool:
        """True iff the t-CI for the mean delta is entirely one side of zero."""
        return self.t_ci_low > 0.0 or self.t_ci_high < 0.0

    @property
    def quality_not_worse(self) -> bool:
        """True iff we cannot say the candidate is *worse* (CI low ≥ 0 … allowing
        ties): the lower CI bound is not below zero by more than rounding."""
        return self.t_ci_low >= 0.0


class CostAnalysis(BaseModel):
    """Per-shot video-second cost comparison + total eval spend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int
    mean_cost_delta_s: float
    candidate_total_video_seconds: float
    production_total_video_seconds: float

    @property
    def candidate_cheaper(self) -> bool:
        """True iff the candidate spends fewer video-seconds per shot on average."""
        return self.mean_cost_delta_s < 0.0


class LatencyAnalysis(BaseModel):
    """Per-shot latency comparison (ms)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    n: int
    mean_latency_delta_ms: float

    @property
    def candidate_faster(self) -> bool:
        """True iff the candidate is faster per shot on average."""
        return self.mean_latency_delta_ms < 0.0


class ReliabilityAnalysis(BaseModel):
    """Non-gated failure rates for both models."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    production: FailureTally
    candidate: FailureTally

    @property
    def failure_rate_delta(self) -> float:
        """``candidate.failure_rate - production.failure_rate`` (negative = better)."""
        return self.candidate.failure_rate - self.production.failure_rate


class ComparisonAnalysis(BaseModel):
    """The full paired analysis of one candidate against production."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_model: str
    production_model: str
    n_samples: int
    quality: QualityAnalysis | None
    cost: CostAnalysis
    latency: LatencyAnalysis
    reliability: ReliabilityAnalysis


def analyze(
    dataset: ComparisonDataset,
    *,
    confidence: float = 0.95,
    win_margin: float = 0.0,
    bootstrap_iterations: int = 2000,
    bootstrap_seed: int = 0,
) -> ComparisonAnalysis:
    """Compute the full paired :class:`ComparisonAnalysis` over ``dataset``.

    ``win_margin`` is the dead-band for the win-rate (a delta within ``±margin`` is
    a tie). ``confidence`` drives every CI. ``bootstrap_seed`` keeps the bootstrap
    interval reproducible. The quality block is ``None`` when there are fewer than
    two comparable pairs (a CI needs spread).
    """
    quality = _quality(
        dataset,
        confidence=confidence,
        win_margin=win_margin,
        bootstrap_iterations=bootstrap_iterations,
        bootstrap_seed=bootstrap_seed,
    )
    cost = _cost(dataset)
    latency = _latency(dataset)
    reliability = ReliabilityAnalysis(
        production=production_failures(dataset),
        candidate=candidate_failures(dataset),
    )
    return ComparisonAnalysis(
        candidate_model=dataset.candidate_model,
        production_model=dataset.production_model,
        n_samples=len(dataset),
        quality=quality,
        cost=cost,
        latency=latency,
        reliability=reliability,
    )


def _quality(
    dataset: ComparisonDataset,
    *,
    confidence: float,
    win_margin: float,
    bootstrap_iterations: int,
    bootstrap_seed: int,
) -> QualityAnalysis | None:
    deltas = dataset.quality_deltas()
    if len(deltas) < 2:
        return None
    ttest = stats.paired_t_test(deltas, confidence=confidence)
    boot = stats.bootstrap_mean_ci(
        deltas,
        confidence=confidence,
        iterations=bootstrap_iterations,
        seed=bootstrap_seed,
    )
    wr = stats.win_rate(deltas, margin=win_margin, confidence=confidence)
    wilcoxon = stats.wilcoxon_signed_rank(deltas)
    return QualityAnalysis(
        n_comparable=len(deltas),
        mean_delta=ttest.mean,
        t_ci_low=ttest.interval.low,
        t_ci_high=ttest.interval.high,
        t_p_value=ttest.p_value,
        bootstrap_ci_low=boot.low,
        bootstrap_ci_high=boot.high,
        win_rate=wr.rate,
        win_ci_low=wr.interval.low,
        win_ci_high=wr.interval.high,
        wins=wr.wins,
        losses=wr.losses,
        ties=wr.ties,
        wilcoxon_p_value=wilcoxon.p_value,
        confidence=confidence,
    )


def _cost(dataset: ComparisonDataset) -> CostAnalysis:
    deltas = dataset.cost_deltas()
    n = len(deltas)
    mean = math.fsum(deltas) / n if n else 0.0
    cand_total = math.fsum(
        s.candidate.video_seconds for s in dataset.samples if not s.candidate.is_gated
    )
    prod_total = math.fsum(
        s.production.video_seconds for s in dataset.samples if not s.production.is_gated
    )
    return CostAnalysis(
        n=n,
        mean_cost_delta_s=mean,
        candidate_total_video_seconds=cand_total,
        production_total_video_seconds=prod_total,
    )


def _latency(dataset: ComparisonDataset) -> LatencyAnalysis:
    deltas = dataset.latency_deltas_ms()
    n = len(deltas)
    mean = math.fsum(deltas) / n if n else 0.0
    return LatencyAnalysis(n=n, mean_latency_delta_ms=mean)


__all__ = [
    "ComparisonAnalysis",
    "CostAnalysis",
    "LatencyAnalysis",
    "QualityAnalysis",
    "ReliabilityAnalysis",
    "analyze",
]
