"""Metric collection — turn raw render outcomes into per-arm statistics.

Every time the render pipeline finishes (or fails) a shot that was assigned to an
experiment arm, it reports one :class:`RenderOutcome`. The :class:`MetricCollector`
folds those outcomes into running, streaming aggregates per arm so a report or a
guardrail check can be produced at any moment without re-scanning history. The
aggregates are deliberately the two shapes the statistics engine consumes:

* a **proportion** (successes / trials) for binary metrics — did the shot fail?
  was the clip accepted into the cut?
* a **streaming mean + variance** (via Welford's algorithm) for continuous
  metrics — the quality score, the cost-per-second, the latency.

Well-known metric keys are derived automatically from the outcome fields (see the
``QUALITY_SCORE`` etc. constants in :mod:`.models`); any extra numeric signals an
outcome carries in ``extra`` are aggregated as continuous means under their own
keys, so a new model-specific metric needs no code change here.

Pure and infra-free: feed it outcomes, read back stats. No clock, no storage, no
RNG. The render pipeline owns *when* to call :meth:`record`; this owns the math.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.flags.stats import ProportionStat, SampleStat
from app.video.experiments.models import (
    ACCEPT_RATE,
    COST_PER_SECOND,
    FAILURE_RATE,
    LATENCY_MS,
    QUALITY_SCORE,
)


@dataclass(frozen=True, slots=True)
class RenderOutcome:
    """One completed (or failed) render attributed to an experiment arm.

    Attributes:
        variant_key: Which arm produced this render.
        succeeded: False when the render failed terminally (after the pipeline's
            own retries/degradation) — feeds the failure-rate guardrail.
        accepted: Whether the resulting clip was accepted into the cut (the
            Critic/Director kept it) — the headline quality proxy. ``None`` when
            acceptance is not yet known (e.g. a hard failure); it is then only
            counted toward ``failure_rate``.
        quality_score: Continuous quality in [0, 1] (e.g. the §13 CCS / aesthetic
            score). ``None`` to skip.
        cost_usd: Provider spend for this render, in dollars. ``None`` to skip.
        duration_s: Clip length in seconds — used to normalize cost into
            cost-per-second (the budget-relevant unit, §11.1). 0/None disables
            the per-second normalization for this outcome.
        latency_ms: Wall-clock generation latency. ``None`` to skip.
        extra: Any additional continuous metrics keyed by metric name.
    """

    variant_key: str
    succeeded: bool = True
    accepted: bool | None = None
    quality_score: float | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    latency_ms: float | None = None
    extra: dict[str, float] = field(default_factory=dict)


@dataclass
class _Welford:
    """Streaming mean/variance (Welford's online algorithm)."""

    count: int = 0
    mean: float = 0.0
    m2: float = 0.0  # sum of squared deviations

    def add(self, value: float) -> None:
        self.count += 1
        delta = value - self.mean
        self.mean += delta / self.count
        self.m2 += delta * (value - self.mean)

    def as_sample(self) -> SampleStat:
        """A :class:`SampleStat` with sample (ddof=1) variance."""
        if self.count == 0:
            return SampleStat(0, 0.0, 0.0)
        if self.count == 1:
            return SampleStat(1, self.mean, 0.0)
        return SampleStat(self.count, self.mean, self.m2 / (self.count - 1))


@dataclass
class _ArmAggregate:
    """All running aggregates for a single arm."""

    # Proportion metrics.
    attempts: int = 0  # denominator for failure_rate
    failures: int = 0
    accept_trials: int = 0  # denominator for accept_rate (outcomes with a known accept verdict)
    accepts: int = 0
    # Continuous metrics (Welford accumulators, keyed by metric name).
    means: dict[str, _Welford] = field(default_factory=dict)

    def _mean(self, key: str) -> _Welford:
        acc = self.means.get(key)
        if acc is None:
            acc = _Welford()
            self.means[key] = acc
        return acc


class MetricCollector:
    """Accumulates :class:`RenderOutcome` s into per-arm statistics.

    The collector is keyed by metric name; ask it for either a
    :class:`ProportionStat` (binary metrics) or a :class:`SampleStat` (continuous
    metrics) for any arm at any time. It does not know the experiment definition —
    the report module pairs the requested metric key with the right accessor.
    """

    def __init__(self) -> None:
        self._arms: dict[str, _ArmAggregate] = {}

    def _arm(self, variant_key: str) -> _ArmAggregate:
        agg = self._arms.get(variant_key)
        if agg is None:
            agg = _ArmAggregate()
            self._arms[variant_key] = agg
        return agg

    def record(self, outcome: RenderOutcome) -> None:
        """Fold one render outcome into its arm's aggregates."""
        arm = self._arm(outcome.variant_key)
        arm.attempts += 1
        if not outcome.succeeded:
            arm.failures += 1

        if outcome.accepted is not None:
            arm.accept_trials += 1
            if outcome.accepted:
                arm.accepts += 1

        if outcome.quality_score is not None:
            arm._mean(QUALITY_SCORE).add(outcome.quality_score)

        if outcome.latency_ms is not None:
            arm._mean(LATENCY_MS).add(outcome.latency_ms)

        if outcome.cost_usd is not None:
            if outcome.duration_s and outcome.duration_s > 0:
                arm._mean(COST_PER_SECOND).add(outcome.cost_usd / outcome.duration_s)
            else:
                # No duration → record raw cost under the same key (best effort).
                arm._mean(COST_PER_SECOND).add(outcome.cost_usd)

        for key, value in outcome.extra.items():
            arm._mean(key).add(value)

    # -- read-back -------------------------------------------------------- #

    def sample_count(self, variant_key: str) -> int:
        """Total render attempts recorded for an arm (the natural sample size)."""
        arm = self._arms.get(variant_key)
        return arm.attempts if arm is not None else 0

    def arms(self) -> tuple[str, ...]:
        """Arms that have at least one recorded outcome."""
        return tuple(self._arms.keys())

    def proportion(self, variant_key: str, metric_key: str) -> ProportionStat:
        """The binomial summary for a proportion metric on an arm.

        Knows the two built-in proportion metrics (``failure_rate``,
        ``accept_rate``); any other key is treated as having zero trials (a
        proportion metric the collector was never fed).
        """
        arm = self._arms.get(variant_key)
        if arm is None:
            return ProportionStat(0, 0)
        if metric_key == FAILURE_RATE:
            return ProportionStat(arm.failures, arm.attempts)
        if metric_key == ACCEPT_RATE:
            return ProportionStat(arm.accepts, arm.accept_trials)
        return ProportionStat(0, 0)

    def mean(self, variant_key: str, metric_key: str) -> SampleStat:
        """The continuous summary (count/mean/variance) for a mean metric on an arm."""
        arm = self._arms.get(variant_key)
        if arm is None:
            return SampleStat(0, 0.0, 0.0)
        acc = arm.means.get(metric_key)
        return acc.as_sample() if acc is not None else SampleStat(0, 0.0, 0.0)

    def rate(self, variant_key: str, metric_key: str) -> float:
        """Convenience: the observed proportion rate for an arm (0 when empty)."""
        return self.proportion(variant_key, metric_key).rate


__all__ = [
    "MetricCollector",
    "RenderOutcome",
]
