"""The rollout state machine — drive an experiment from ramp to a final call.

A :class:`VideoExperiment` definition + a :class:`MetricCollector` of outcomes is
inert; the :class:`ExperimentRunner` is what *moves*. On each :meth:`evaluate`
tick (the render pipeline or scheduler calls it periodically) the runner:

#. builds the current decision report (:mod:`.report`);
#. if any guardrail breached → transitions to ``ROLLED_BACK`` immediately and
   pins traffic back to 0% (control only) — *safety dominates and is one-way*;
#. else if a treatment is a clear, significant winner past the sample floor →
   transitions to ``PROMOTED`` (the caller swaps the new model in as default);
#. else if the wall-clock budget (``max_duration_s``) is spent → ``CONCLUDED``
   with whatever the data says (HOLD = keep the incumbent);
#. else stays ``RAMPING``/``HOLDING`` and collects more data.

A progressive **canary** is the same machinery with one extra rule: instead of
holding a fixed arm split, it grows enrollment along a ladder (1% → 5% → 25% →
100%), advancing a rung only when the current rung is clean and has enough data,
and *halting* (rolling back) the instant a guardrail breaches at any rung. That
is the standard "expose a little, watch, expose more" rollout and it reuses the
exact same report + guardrail logic as the A/B runner.

Determinism: the only time source is an injectable ``clock`` (a ``() -> float``
returning monotonic seconds). Tests drive a fake clock; production passes
``time.monotonic``. No RNG, no real sleeps, no storage.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.logging import get_logger
from app.video.experiments.assignment import RenderUnit, VideoAssigner, VideoAssignment
from app.video.experiments.metrics import MetricCollector, RenderOutcome
from app.video.experiments.models import VideoExperiment
from app.video.experiments.report import ExperimentReport, Recommendation, build_report

logger = get_logger("app.video.experiments.runner")

#: Monotonic-seconds clock seam. Production: ``time.monotonic``.
ClockFn = Callable[[], float]


class RolloutState(StrEnum):
    """Lifecycle of a rollout (A/B or canary)."""

    RAMPING = "ramping"  # growing exposure, watching guardrails
    HOLDING = "holding"  # at target exposure, accruing data for a decision
    PROMOTED = "promoted"  # a winner was promoted; terminal-success
    ROLLED_BACK = "rolled_back"  # a guardrail breached; terminal-failure, traffic pinned to 0
    CONCLUDED = "concluded"  # max duration spent without a promote/rollback (incumbent stays)

    @property
    def is_terminal(self) -> bool:
        return self in (RolloutState.PROMOTED, RolloutState.ROLLED_BACK, RolloutState.CONCLUDED)


@dataclass(frozen=True, slots=True)
class RolloutDecision:
    """The outcome of one :meth:`ExperimentRunner.evaluate` tick."""

    state: RolloutState
    traffic_percent: float
    report: ExperimentReport
    changed: bool  # did the state or traffic move on this tick?
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {
            "state": self.state.value,
            "traffic_percent": self.traffic_percent,
            "changed": self.changed,
            "detail": self.detail,
            "report": self.report.to_dict(),
        }


@dataclass
class ExperimentRunner:
    """Drives one A/B experiment from ramp to a terminal call.

    The runner *owns* the live ``traffic_percent`` (so assignment honors the
    current exposure) and the current :class:`RolloutState`. It does not own the
    collector — the render pipeline feeds outcomes via :meth:`record` and ticks
    :meth:`evaluate`. The experiment definition's arm split is fixed; only the
    enrollment percentage moves (target ``target_percent``, default full).
    """

    experiment: VideoExperiment
    clock: ClockFn
    target_percent: float = 100.0
    alpha: float = 0.05
    guardrail_alpha: float = 0.01

    # -- mutable state (not part of the constructor signature below) ------ #
    state: RolloutState = field(default=RolloutState.RAMPING, init=False)
    _traffic: float = field(default=0.0, init=False)
    _started_at: float | None = field(default=None, init=False)
    _collector: MetricCollector = field(default_factory=MetricCollector, init=False)

    def __post_init__(self) -> None:
        if not 0.0 < self.target_percent <= 100.0:
            raise ValueError("target_percent must be in (0, 100]")
        # Start exposed at the target immediately for a plain A/B (no ramp ladder);
        # the canary subclass overrides this to start at the first rung.
        self._traffic = self.target_percent

    # -- exposure / assignment ------------------------------------------- #

    @property
    def traffic_percent(self) -> float:
        return self._traffic

    def _live_experiment(self) -> VideoExperiment:
        """The experiment definition at the *current* enrollment percentage."""
        if self._traffic == self.experiment.traffic_percent:
            return self.experiment
        return self.experiment.with_traffic_percent(self._traffic)

    def assigner(self) -> VideoAssigner:
        """An assigner honoring the runner's current exposure (terminal-aware).

        Once ``ROLLED_BACK``/``CONCLUDED`` (without a promote) exposure is pinned
        to 0% so every unit falls back to control behavior.
        """
        traffic = 0.0 if self.state is RolloutState.ROLLED_BACK else self._traffic
        exp = (
            self.experiment
            if traffic == self.experiment.traffic_percent
            else self.experiment.with_traffic_percent(traffic)
        )
        return VideoAssigner(exp)

    def assign(self, unit: RenderUnit) -> VideoAssignment:
        """Assign one render unit at the current exposure."""
        return self.assigner().assign(unit)

    # -- outcome intake --------------------------------------------------- #

    def record(self, outcome: RenderOutcome) -> None:
        """Fold one render outcome into the running aggregates."""
        if self._started_at is None:
            self._started_at = self.clock()
        self._collector.record(outcome)

    @property
    def collector(self) -> MetricCollector:
        return self._collector

    def elapsed_s(self) -> float:
        """Seconds since the first recorded outcome (0 before any)."""
        if self._started_at is None:
            return 0.0
        return max(0.0, self.clock() - self._started_at)

    # -- the tick --------------------------------------------------------- #

    def evaluate(self) -> RolloutDecision:
        """Advance the state machine one tick against the current data."""
        report = build_report(
            self.experiment,
            self._collector,
            alpha=self.alpha,
            guardrail_alpha=self.guardrail_alpha,
        )
        if self.state.is_terminal:
            return RolloutDecision(self.state, self.traffic_percent, report, False, "terminal")

        # 1) Safety: a guardrail breach is a one-way trip to ROLLED_BACK.
        if report.recommendation is Recommendation.ROLLBACK:
            return self._transition(
                RolloutState.ROLLED_BACK,
                0.0,
                report,
                f"guardrail breach → rollback ({', '.join(report.rollback_keys)})",
            )

        # 2) Winner: promote and finish.
        if report.recommendation is Recommendation.PROMOTE and report.winner_key is not None:
            return self._transition(
                RolloutState.PROMOTED,
                self.traffic_percent,
                report,
                f"promoted winner {report.winner_key}: {report.rationale}",
            )

        # 3) Time budget spent → conclude (incumbent stays).
        if self.elapsed_s() >= self.experiment.max_duration_s:
            return self._transition(
                RolloutState.CONCLUDED,
                self.traffic_percent,
                report,
                f"max duration {self.experiment.max_duration_s:.0f}s reached; "
                f"{report.rationale}",
            )

        # 4) Hook for ramping subclasses; base A/B just holds at target.
        return self._tick_active(report)

    def _tick_active(self, report: ExperimentReport) -> RolloutDecision:
        """Non-terminal tick for a plain A/B: settle into HOLDING."""
        if self.state is RolloutState.RAMPING:
            return self._transition(
                RolloutState.HOLDING,
                self.traffic_percent,
                report,
                "at target exposure; holding for a decision",
            )
        return RolloutDecision(
            self.state, self.traffic_percent, report, False, "holding; collecting data"
        )

    # -- helpers ---------------------------------------------------------- #

    def _transition(
        self,
        state: RolloutState,
        traffic: float,
        report: ExperimentReport,
        detail: str,
    ) -> RolloutDecision:
        changed = state is not self.state or traffic != self._traffic
        if changed:
            logger.info(
                "video_experiment_transition",
                experiment=self.experiment.key,
                from_state=self.state.value,
                to_state=state.value,
                traffic_percent=traffic,
                detail=detail,
            )
        self.state = state
        self._traffic = traffic
        return RolloutDecision(state, traffic, report, changed, detail)


# --------------------------------------------------------------------------- #
# Progressive canary
# --------------------------------------------------------------------------- #

#: The standard exposure ladder for a canary rollout (percent of eligible traffic).
DEFAULT_CANARY_LADDER: tuple[float, ...] = (1.0, 5.0, 25.0, 100.0)


@dataclass
class CanaryRunner(ExperimentRunner):
    """A progressive canary: grow exposure rung-by-rung, halt on regression.

    Built directly on :class:`ExperimentRunner` — same assignment, same
    collector, same report + guardrail logic. The only behavioral addition is the
    exposure **ladder**: the runner starts at the first rung (e.g. 1%) and, on a
    clean tick with enough data at the current rung, advances to the next rung;
    it reaches ``PROMOTED`` only after a clean top rung (100%), and ``ROLLED_BACK``
    the instant any guardrail breaches at any rung.

    ``min_samples_per_rung`` is the per-arm data each rung must accrue before
    advancing (defaults to the experiment's ``min_samples_per_arm`` so the same
    statistical floor governs both promotion and rung advancement).
    """

    ladder: tuple[float, ...] = DEFAULT_CANARY_LADDER
    min_samples_per_rung: int | None = None

    _rung: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        if not self.ladder:
            raise ValueError("canary ladder must be non-empty")
        if any(p <= 0 or p > 100 for p in self.ladder):
            raise ValueError("canary ladder rungs must be in (0, 100]")
        if list(self.ladder) != sorted(self.ladder):
            raise ValueError("canary ladder must be non-decreasing")
        self.state = RolloutState.RAMPING
        self._rung = 0
        self._traffic = self.ladder[0]
        self._started_at = None
        if self.min_samples_per_rung is None:
            self.min_samples_per_rung = self.experiment.min_samples_per_arm

    @property
    def rung_index(self) -> int:
        """Index of the current ladder rung (0-based)."""
        return self._rung

    @property
    def at_top_rung(self) -> bool:
        return self._rung >= len(self.ladder) - 1

    def _rung_has_enough_data(self) -> bool:
        """Every arm has at least ``min_samples_per_rung`` samples at this rung."""
        floor = self.min_samples_per_rung or 1
        return all(
            self._collector.sample_count(v.key) >= floor for v in self.experiment.variants
        )

    def _tick_active(self, report: ExperimentReport) -> RolloutDecision:
        """Ramp the canary: advance a rung when clean + enough data; else hold."""
        # On a fully clean top rung with enough data, promotion is handled by the
        # base evaluate() winner branch; if there's no significant winner but the
        # top rung is clean and saturated, we conclude success (the new model is
        # safe at 100%, even if not a *statistically* proven improvement).
        if self.at_top_rung:
            if self._rung_has_enough_data():
                return self._transition(
                    RolloutState.PROMOTED,
                    self._traffic,
                    report,
                    "canary reached 100% clean; promoting (no guardrail breach)",
                )
            if self.state is RolloutState.RAMPING:
                return self._transition(
                    RolloutState.HOLDING,
                    self._traffic,
                    report,
                    "at top rung; holding for data",
                )
            return RolloutDecision(
                self.state, self._traffic, report, False, "top rung; collecting data"
            )

        # Not yet at the top: advance only on a clean rung with enough data.
        if self._rung_has_enough_data():
            self._rung += 1
            next_pct = self.ladder[self._rung]
            return self._transition(
                RolloutState.RAMPING,
                next_pct,
                report,
                f"rung clean; advancing to {next_pct:.0f}% (rung {self._rung})",
            )
        return RolloutDecision(
            self.state,
            self._traffic,
            report,
            False,
            f"rung {self._rung} ({self._traffic:.0f}%): collecting data",
        )


__all__ = [
    "DEFAULT_CANARY_LADDER",
    "CanaryRunner",
    "ClockFn",
    "ExperimentRunner",
    "RolloutDecision",
    "RolloutState",
]
