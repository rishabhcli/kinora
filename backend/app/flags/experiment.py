"""The pure experiment framework — assignment, exposure, and metric specs.

An :class:`Experiment` layers an A/B/n test on top of the bucketing primitive: it
splits an eligible population across :class:`Variant`\\ s, assigns each unit
deterministically (so re-runs and multiple processes agree), and emits a
de-duplicated *exposure* record the moment a unit is actually shown its arm.
Metrics (:class:`Metric`) describe what to measure — a primary success metric and
any number of guardrails — but the *math* lives in :mod:`app.flags.stats`; this
module only structures the inputs.

Pure and infra-free: assignment is a function of ``(experiment, context)`` and
exposure de-dup is a deterministic key, so the engine can run in a worker, a
test, or behind the SDK with no storage at all (storage merely persists the
exposures the engine generates).

Maps to kinora.md §13: the crew-vs-baseline study is exactly a two-arm
experiment (``control`` = single-agent, ``treatment`` = crew+memory) over a fixed
N, and the watermark-tuning study (§18 Q4) is another.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.flags.context import EvalContext
from app.flags.errors import ExperimentValidationError
from app.flags.hashing import TOTAL_BASIS_POINTS, bucket_bp, in_rollout, weighted_index
from app.flags.models import Rule
from app.flags.targeting import rule_matches


class ExperimentStatus(StrEnum):
    """Lifecycle of an experiment."""

    DRAFT = "draft"  # editable, not assigning
    RUNNING = "running"  # assigning + logging exposures
    PAUSED = "paused"  # not assigning new units (sticky for assigned ones)
    CONCLUDED = "concluded"  # decision made; assignment frozen


class MetricDirection(StrEnum):
    """Whether a higher metric value is good or bad."""

    INCREASE = "increase"  # higher is better (conversion, completion)
    DECREASE = "decrease"  # lower is better (latency, stalls, regen rate)


class MetricKind(StrEnum):
    """How a metric is aggregated."""

    PROPORTION = "proportion"  # binary success rate (binomial)
    MEAN = "mean"  # continuous average


@dataclass(frozen=True, slots=True)
class Metric:
    """A measurable outcome the experiment tracks."""

    key: str
    kind: MetricKind = MetricKind.PROPORTION
    direction: MetricDirection = MetricDirection.INCREASE
    is_guardrail: bool = False
    #: For guardrails: tolerated relative regression before a breach (e.g. 0.02).
    guardrail_margin: float = 0.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise ExperimentValidationError("metric.key must be non-empty")
        if self.guardrail_margin < 0:
            raise ExperimentValidationError("guardrail_margin must be >= 0")


@dataclass(frozen=True, slots=True)
class Variant:
    """One arm of an experiment with an integer basis-point allocation."""

    key: str
    weight: int  # basis points
    is_control: bool = False
    #: Optional flag variation this arm maps to (so the SDK can serve a value).
    flag_variation: str | None = None

    def __post_init__(self) -> None:
        if not self.key:
            raise ExperimentValidationError("variant.key must be non-empty")
        if self.weight < 0:
            raise ExperimentValidationError("variant.weight must be >= 0")


@dataclass(frozen=True, slots=True)
class Experiment:
    """A self-validating A/B/n experiment definition."""

    key: str
    variants: tuple[Variant, ...]
    salt: str
    status: ExperimentStatus = ExperimentStatus.DRAFT
    #: Eligibility rule: only units matching these clauses enter the experiment.
    audience: tuple[Rule, ...] = ()
    #: Fraction of the eligible population actually enrolled (the rest see control
    #: behavior but are not logged) — lets you ramp exposure independently of the
    #: arm split. 0..100.
    traffic_percent: float = 100.0
    bucket_by: str | None = None
    metrics: tuple[Metric, ...] = ()
    version: int = 1
    name: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise ExperimentValidationError("experiment.key must be non-empty")
        if not self.salt:
            raise ExperimentValidationError("experiment.salt must be non-empty")
        if len(self.variants) < 2:
            raise ExperimentValidationError(
                f"experiment {self.key!r} needs at least two variants"
            )
        keys = [v.key for v in self.variants]
        if len(set(keys)) != len(keys):
            raise ExperimentValidationError(f"experiment {self.key!r} has duplicate variant keys")
        total = sum(v.weight for v in self.variants)
        if total != TOTAL_BASIS_POINTS:
            raise ExperimentValidationError(
                f"experiment {self.key!r} variant weights must sum to {TOTAL_BASIS_POINTS} bp "
                f"(got {total})"
            )
        controls = [v for v in self.variants if v.is_control]
        if len(controls) != 1:
            raise ExperimentValidationError(
                f"experiment {self.key!r} must have exactly one control variant"
            )
        if not 0.0 <= self.traffic_percent <= 100.0:
            raise ExperimentValidationError("traffic_percent must be in [0, 100]")
        metric_keys = [m.key for m in self.metrics]
        if len(set(metric_keys)) != len(metric_keys):
            raise ExperimentValidationError(f"experiment {self.key!r} has duplicate metric keys")

    @property
    def control(self) -> Variant:
        """The control arm (exactly one, validated)."""
        return next(v for v in self.variants if v.is_control)

    @property
    def primary_metric(self) -> Metric | None:
        """The first non-guardrail metric, if any."""
        return next((m for m in self.metrics if not m.is_guardrail), None)

    @property
    def guardrails(self) -> tuple[Metric, ...]:
        """All guardrail metrics."""
        return tuple(m for m in self.metrics if m.is_guardrail)

    def variant_by_key(self, key: str) -> Variant:
        for v in self.variants:
            if v.key == key:
                return v
        raise ExperimentValidationError(f"experiment {self.key!r} has no variant {key!r}")


@dataclass(frozen=True, slots=True)
class Assignment:
    """The result of assigning one context to an experiment."""

    experiment_key: str
    variant_key: str | None  # None when not enrolled
    in_experiment: bool
    reason: str
    bucket: int
    experiment_version: int = 0

    @property
    def is_control(self) -> bool:
        return self.reason == "assigned" and self.variant_key is not None


@dataclass
class ExperimentEngine:
    """Assigns contexts to experiment arms and de-duplicates exposures.

    Assignment salts: the *enrollment* decision (am I in the traffic slice?) and
    the *arm* decision (which variant?) use **different** derived salts so a unit
    that is "early" in the traffic ramp is not also biased toward a particular
    arm. Both derive from the experiment's own ``salt``.
    """

    experiment: Experiment

    def _enroll_salt(self) -> str:
        return f"{self.experiment.salt}:enroll"

    def _arm_salt(self) -> str:
        return f"{self.experiment.salt}:arm"

    def assign(self, context: EvalContext) -> Assignment:
        """Deterministically assign ``context`` to an arm (or not enrolled)."""
        exp = self.experiment
        bucket = bucket_bp(context.unit_for(exp.bucket_by), self._arm_salt())

        if exp.status in (ExperimentStatus.DRAFT, ExperimentStatus.CONCLUDED):
            return self._miss("not_running", bucket)
        if not self._audience_ok(context):
            return self._miss("audience_excluded", bucket)
        unit = context.unit_for(exp.bucket_by)
        if not in_rollout(unit, self._enroll_salt(), exp.traffic_percent):
            return self._miss("traffic_excluded", bucket)

        weights = tuple(v.weight for v in exp.variants)
        index = weighted_index(unit, self._arm_salt(), weights)
        variant = exp.variants[index]
        return Assignment(
            experiment_key=exp.key,
            variant_key=variant.key,
            in_experiment=True,
            reason="assigned",
            bucket=bucket,
            experiment_version=exp.version,
        )

    def _audience_ok(self, context: EvalContext) -> bool:
        if not self.experiment.audience:
            return True
        # Audience rules are ORed: matching any one admits the unit.
        return any(rule_matches(rule, context) for rule in self.experiment.audience)

    def _miss(self, reason: str, bucket: int) -> Assignment:
        return Assignment(
            experiment_key=self.experiment.key,
            variant_key=None,
            in_experiment=False,
            reason=reason,
            bucket=bucket,
            experiment_version=self.experiment.version,
        )

    def exposure_key(self, context: EvalContext, assignment: Assignment) -> str | None:
        """A stable de-dup key for one (unit, experiment, version) exposure.

        ``None`` when the unit is not enrolled or is anonymous (anonymous units
        bucket deterministically but carry no durable identity to attribute an
        exposure to). Logging the same key twice is a no-op at the sink — so an
        exposure is counted at most once per unit per experiment version.
        """
        if not assignment.in_experiment or context.anonymous:
            return None
        return (
            f"{self.experiment.key}:v{self.experiment.version}:"
            f"{context.unit_for(self.experiment.bucket_by)}"
        )


def expected_allocation(experiment: Experiment) -> dict[str, float]:
    """The fraction of enrolled traffic each arm should receive (for QA/diagnostics)."""
    return {v.key: v.weight / TOTAL_BASIS_POINTS for v in experiment.variants}


__all__ = [
    "Assignment",
    "Experiment",
    "ExperimentEngine",
    "ExperimentStatus",
    "Metric",
    "MetricDirection",
    "MetricKind",
    "Variant",
    "expected_allocation",
]
