"""Definition objects for video-model A/B experiments and canary rollouts.

A new video model (a faster Wan turbo id, a MiniMax Hailuo revision, a different
provider entirely) shows up roughly every few weeks. Before it carries reader
traffic we want to *prove* it is at least as good as the model it replaces —
cheaper, faster, more reliable, and at least as good-looking — and we want to be
able to back it out instantly if it regresses. That is an experiment.

This module is the pure **definition** layer. A :class:`VideoVariant` is one
fully-specified render configuration (which provider, which model id, which
generation parameters) the Generator could call. A :class:`VideoExperiment`
splits eligible render traffic across a control variant and one or more
treatments, declares which metric decides the winner, which metrics may never
regress (guardrails), and the stopping rules (minimum sample size, maximum
duration). Everything here is frozen, self-validating, and infra-free — the
*assignment* lives in :mod:`.assignment`, the *math* in :mod:`.statistics`, and
the *rollout state machine* in :mod:`.runner`.

Design notes
------------
* Allocation is in **basis points** (1 bp = 0.01%) so an arm split is exact in
  integer math and reuses the repo's proven :mod:`app.flags.hashing` bucketing.
* A variant is identified by its ``key``; its ``spec`` is an opaque, hashable
  map of render knobs (``{"resolution": "1080P", "duration_s": 8}``) so this
  layer never needs to know the provider's wire format.
* Metric direction matters: a *cost* or *latency* metric is ``DECREASE`` (lower
  is better) while *quality* or *accept-rate* is ``INCREASE``. Guardrail breach
  logic in :mod:`.statistics` reads the direction so "regression" always means
  "moved the wrong way".
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from app.flags.hashing import TOTAL_BASIS_POINTS

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class VideoExperimentError(ValueError):
    """A video experiment / variant / metric definition is invalid."""


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #


class MetricDirection(StrEnum):
    """Which way is *good* for a metric."""

    INCREASE = "increase"  # quality score, accepted-footage rate, completion
    DECREASE = "decrease"  # cost per second, latency, failure rate, regen rate


class MetricKind(StrEnum):
    """How a metric is aggregated across samples."""

    PROPORTION = "proportion"  # binary success rate (binomial) — accept rate, failure rate
    MEAN = "mean"  # continuous average — quality score, cost, latency


#: Canonical metric keys the collector knows how to derive from a render outcome
#: (see :mod:`.metrics`). Custom keys are allowed; these are the well-known ones.
QUALITY_SCORE = "quality_score"
ACCEPT_RATE = "accept_rate"
FAILURE_RATE = "failure_rate"
COST_PER_SECOND = "cost_per_second"
LATENCY_MS = "latency_ms"


@dataclass(frozen=True, slots=True)
class VideoMetric:
    """A measurable outcome of a video render that an experiment tracks.

    Attributes:
        key: Stable identifier (use the ``QUALITY_SCORE`` etc. constants for the
            well-known ones, or any custom string).
        kind: PROPORTION (binomial) or MEAN (continuous).
        direction: Whether higher or lower is better.
        is_guardrail: A guardrail can only *halt/rollback* an arm; it never
            promotes one. The primary metric is the (single) non-guardrail
            metric that decides the winner.
        guardrail_margin: For guardrails — the tolerated *relative* regression
            before a breach (``0.05`` tolerates a 5% relative slip). Ignored for
            non-guardrail metrics.
        name: Human label for reports.
    """

    key: str
    kind: MetricKind = MetricKind.MEAN
    direction: MetricDirection = MetricDirection.INCREASE
    is_guardrail: bool = False
    guardrail_margin: float = 0.0
    name: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise VideoExperimentError("metric.key must be non-empty")
        if self.guardrail_margin < 0:
            raise VideoExperimentError("metric.guardrail_margin must be >= 0")


# --------------------------------------------------------------------------- #
# Variants
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VideoVariant:
    """One render configuration an experiment can route a shot to.

    ``spec`` is the opaque parameter set the Generator resolves into a provider
    call (e.g. ``{"resolution": "1080P", "duration_s": 8, "prompt_extend": True}``).
    It is stored as a read-only mapping so the frozen variant stays hashable-ish
    and cannot be mutated after construction.

    Attributes:
        key: Stable arm identity (used for sticky assignment + reporting).
        provider: Backend family the variant renders on (``"dashscope"``,
            ``"minimax"``, a future region…). Telemetry/cost attribution only.
        model: The concrete model id (``"wan2.1-t2v-turbo"``, ``"MiniMax-Hailuo-2.3"``).
        weight: Basis-point share of enrolled traffic (arms sum to 10_000).
        is_control: Exactly one variant per experiment is the control baseline.
        spec: Opaque render-parameter overrides for this arm.
    """

    key: str
    provider: str
    model: str
    weight: int = 0
    is_control: bool = False
    spec: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.key:
            raise VideoExperimentError("variant.key must be non-empty")
        if not self.provider:
            raise VideoExperimentError(f"variant {self.key!r} must name a provider")
        if not self.model:
            raise VideoExperimentError(f"variant {self.key!r} must name a model")
        if self.weight < 0:
            raise VideoExperimentError(f"variant {self.key!r} weight must be >= 0")
        # Freeze the spec into a read-only mapping (defensive copy).
        object.__setattr__(self, "spec", MappingProxyType(dict(self.spec)))

    def describe(self) -> str:
        """A compact ``provider/model`` label for logs and reports."""
        return f"{self.provider}/{self.model}"

    def spec_dict(self) -> dict[str, Any]:
        """A plain-``dict`` copy of the render spec (JSON-friendly)."""
        return dict(self.spec)


# --------------------------------------------------------------------------- #
# Targeting
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Targeting:
    """Eligibility filter deciding which renders may enter an experiment.

    All non-empty clauses must match (AND). Empty everywhere = match all. This
    keeps targeting deterministic and infra-free — it reads only fields carried
    on the :class:`~app.video.experiments.assignment.RenderUnit`.

    Attributes:
        modes: If non-empty, only these Wan modes (``"t2v"``, ``"i2v"``…) enter.
        book_ids: If non-empty, only these books enter (e.g. a pilot cohort).
        resolutions: If non-empty, only these requested resolutions enter.
        min_duration_s / max_duration_s: Inclusive bounds on requested clip
            length; ``None`` disables that bound.
    """

    modes: frozenset[str] = frozenset()
    book_ids: frozenset[str] = frozenset()
    resolutions: frozenset[str] = frozenset()
    min_duration_s: float | None = None
    max_duration_s: float | None = None

    def matches(
        self,
        *,
        mode: str | None = None,
        book_id: str | None = None,
        resolution: str | None = None,
        duration_s: float | None = None,
    ) -> bool:
        """Whether a render with these attributes is eligible."""
        if self.modes and (mode is None or mode not in self.modes):
            return False
        if self.book_ids and (book_id is None or book_id not in self.book_ids):
            return False
        if self.resolutions and (resolution is None or resolution not in self.resolutions):
            return False
        if self.min_duration_s is not None and (
            duration_s is None or duration_s < self.min_duration_s
        ):
            return False
        return not (
            self.max_duration_s is not None
            and (duration_s is None or duration_s > self.max_duration_s)
        )


# --------------------------------------------------------------------------- #
# Experiment definition
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class VideoExperiment:
    """A self-validating A/B/n experiment over video-render configurations.

    Attributes:
        key: Stable experiment identity.
        variants: Control + N treatments; weights sum to 10_000 bp.
        salt: Hash salt for sticky assignment (independent per experiment).
        metrics: The primary metric (exactly one non-guardrail) + any guardrails.
        traffic_percent: Fraction of *eligible* renders enrolled (the rest stay
            on control behavior, unlogged). Lets exposure ramp independently of
            the arm split — the canary runner drives this.
        targeting: Eligibility filter (mode/book/resolution/duration).
        bucket_by: Which unit field provides the sticky bucketing key
            (``"book_id"`` keeps a whole book on one model; ``"shot_id"`` varies
            per shot). Defaults to ``"book_id"`` so a reader never sees the model
            flip mid-book.
        min_samples_per_arm: Stopping floor — no winner/rollback decision is made
            until each compared arm has at least this many samples.
        max_duration_s: Stopping ceiling (wall-clock seconds) after which the
            experiment auto-concludes regardless of significance.
        name / description: Human metadata.
    """

    key: str
    variants: tuple[VideoVariant, ...]
    salt: str
    metrics: tuple[VideoMetric, ...] = ()
    traffic_percent: float = 100.0
    targeting: Targeting = field(default_factory=Targeting)
    bucket_by: str = "book_id"
    min_samples_per_arm: int = 100
    max_duration_s: float = 14.0 * 24 * 3600  # two weeks
    name: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if not self.key:
            raise VideoExperimentError("experiment.key must be non-empty")
        if not self.salt:
            raise VideoExperimentError(f"experiment {self.key!r} salt must be non-empty")
        if len(self.variants) < 2:
            raise VideoExperimentError(
                f"experiment {self.key!r} needs at least two variants (control + treatment)"
            )
        keys = [v.key for v in self.variants]
        if len(set(keys)) != len(keys):
            raise VideoExperimentError(f"experiment {self.key!r} has duplicate variant keys")
        total = sum(v.weight for v in self.variants)
        if total != TOTAL_BASIS_POINTS:
            raise VideoExperimentError(
                f"experiment {self.key!r} variant weights must sum to {TOTAL_BASIS_POINTS} bp "
                f"(got {total})"
            )
        controls = [v for v in self.variants if v.is_control]
        if len(controls) != 1:
            raise VideoExperimentError(
                f"experiment {self.key!r} must have exactly one control variant"
            )
        if not 0.0 <= self.traffic_percent <= 100.0:
            raise VideoExperimentError(
                f"experiment {self.key!r} traffic_percent must be in [0, 100]"
            )
        metric_keys = [m.key for m in self.metrics]
        if len(set(metric_keys)) != len(metric_keys):
            raise VideoExperimentError(f"experiment {self.key!r} has duplicate metric keys")
        primaries = [m for m in self.metrics if not m.is_guardrail]
        if self.metrics and len(primaries) != 1:
            raise VideoExperimentError(
                f"experiment {self.key!r} must declare exactly one primary (non-guardrail) "
                f"metric (got {len(primaries)})"
            )
        if self.min_samples_per_arm < 1:
            raise VideoExperimentError(
                f"experiment {self.key!r} min_samples_per_arm must be >= 1"
            )
        if self.max_duration_s <= 0:
            raise VideoExperimentError(f"experiment {self.key!r} max_duration_s must be > 0")

    @property
    def control(self) -> VideoVariant:
        """The single control variant (validated to exist)."""
        return next(v for v in self.variants if v.is_control)

    @property
    def treatments(self) -> tuple[VideoVariant, ...]:
        """Every non-control arm."""
        return tuple(v for v in self.variants if not v.is_control)

    @property
    def primary_metric(self) -> VideoMetric | None:
        """The metric that decides the winner (the one non-guardrail metric)."""
        return next((m for m in self.metrics if not m.is_guardrail), None)

    @property
    def guardrails(self) -> tuple[VideoMetric, ...]:
        """Every guardrail metric (may halt/rollback an arm, never promote it)."""
        return tuple(m for m in self.metrics if m.is_guardrail)

    def variant(self, key: str) -> VideoVariant:
        """Look up an arm by key (raises if unknown)."""
        for v in self.variants:
            if v.key == key:
                return v
        raise VideoExperimentError(f"experiment {self.key!r} has no variant {key!r}")

    def with_traffic_percent(self, percent: float) -> VideoExperiment:
        """A copy at a new enrollment percentage (used by the canary ramp)."""
        return _replace_experiment(self, traffic_percent=percent)


def _replace_experiment(exp: VideoExperiment, **changes: Any) -> VideoExperiment:
    """``dataclasses.replace`` for the frozen experiment (keeps validation)."""
    from dataclasses import replace

    return replace(exp, **changes)


def expected_allocation(experiment: VideoExperiment) -> dict[str, float]:
    """Fraction of enrolled traffic each arm should receive (for QA/diagnostics)."""
    return {v.key: v.weight / TOTAL_BASIS_POINTS for v in experiment.variants}


__all__ = [
    "ACCEPT_RATE",
    "COST_PER_SECOND",
    "FAILURE_RATE",
    "LATENCY_MS",
    "QUALITY_SCORE",
    "MetricDirection",
    "MetricKind",
    "Targeting",
    "VideoExperiment",
    "VideoExperimentError",
    "VideoMetric",
    "VideoVariant",
    "expected_allocation",
]
