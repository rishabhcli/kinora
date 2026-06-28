"""Offline policy A/B framework (kinora.md §13/§4.5/§4.6).

The §13 eval protocol fixes the inputs (sequence, seeds, prompts) and compares
two *arms* on pre-registered metrics. This module is the Scheduler's slice of
that: replay a fixed set of reading traces (the :class:`ReaderProfile` archetypes
from the harness) under two :class:`SchedulerPolicy` arms and report the
**buffer-health deltas** — the §13 buffer-health numbers (fraction above ``L``,
visible stalls), the would-be committed video, and the promotion/keyframe counts.

It runs entirely offline through :func:`app.scheduler.simulation.replay_trace`, so
it spends **zero video-seconds** by construction (asserted in the result), and is
fully deterministic: the same trace set + policies → the same report, every time.
This is what lets a policy change (deeper watermarks, adaptive on, a new commit
horizon) be *proven* better before it ships, instead of A/B'd in production on the
scarce video budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import fmean

from app.core.config import Settings, get_settings
from app.core.logging import get_logger
from app.eval.metrics import buffer_health
from app.scheduler.policy import SchedulerPolicy
from app.scheduler.service import ShotSource
from app.scheduler.simulation import ReaderProfile, ReadingTrace, replay_trace

logger = get_logger("app.scheduler.experiment")


def default_trace_suite() -> list[ReadingTrace]:
    """The canonical archetype suite an A/B is scored over (§4.11).

    One trace per §4.11 failure-mode row, so a policy is judged on the whole
    behavioural envelope — steady, fast, jittery, skimming, thinking, seeking —
    not just the easy constant-velocity case.
    """
    return [
        ReaderProfile.steady(velocity_wps=4.0, duration_s=180.0),
        ReaderProfile.steady(velocity_wps=8.0, duration_s=120.0),
        ReaderProfile.variable(base_wps=4.0, jitter=0.5, segments=12, seed=7),
        ReaderProfile.thinker(velocity_wps=3.0, read_s=30.0, pause_s=20.0, cycles=3),
        ReaderProfile.seeker(velocity_wps=4.0, read_s=30.0, jumps=(2000, 200, 3500)),
    ]


@dataclass(slots=True)
class TraceScore:
    """One trace's buffer-health verdict under one policy (§13)."""

    label: str
    fraction_above_low: float
    stalls: int
    peak_committed_s: float
    committed_promotions: int
    keyframes_ensured: int
    simulated_earmarks_s: float
    video_seconds_spent: float


@dataclass(slots=True)
class ArmReport:
    """One policy arm's aggregate over the trace suite (§13)."""

    policy: str
    scores: list[TraceScore] = field(default_factory=list)

    @property
    def mean_fraction_above_low(self) -> float:
        return fmean(s.fraction_above_low for s in self.scores) if self.scores else 0.0

    @property
    def total_stalls(self) -> int:
        return sum(s.stalls for s in self.scores)

    @property
    def total_simulated_earmarks_s(self) -> float:
        return round(sum(s.simulated_earmarks_s for s in self.scores), 6)

    @property
    def total_video_seconds_spent(self) -> float:
        # Invariant: always 0.0 (the harness renders nothing).
        return round(sum(s.video_seconds_spent for s in self.scores), 6)


@dataclass(slots=True)
class ABResult:
    """The A vs B comparison + the headline deltas (§13)."""

    control: ArmReport
    treatment: ArmReport

    @property
    def delta_fraction_above_low(self) -> float:
        """Treatment − control buffer-health (positive = treatment is smoother)."""
        return round(
            self.treatment.mean_fraction_above_low - self.control.mean_fraction_above_low, 6
        )

    @property
    def delta_stalls(self) -> int:
        """Treatment − control stalls (negative = treatment stalls less)."""
        return self.treatment.total_stalls - self.control.total_stalls

    @property
    def delta_earmarks_s(self) -> float:
        """Treatment − control would-be committed video (the budget trade-off)."""
        return round(
            self.treatment.total_simulated_earmarks_s - self.control.total_simulated_earmarks_s, 6
        )

    def summary(self) -> dict[str, object]:
        """A compact dict for logging / the §13 metrics panel."""
        return {
            "control": self.control.policy,
            "treatment": self.treatment.policy,
            "delta_fraction_above_low": self.delta_fraction_above_low,
            "delta_stalls": self.delta_stalls,
            "delta_earmarks_s": self.delta_earmarks_s,
            "control_video_spent": self.control.total_video_seconds_spent,
            "treatment_video_spent": self.treatment.total_video_seconds_spent,
        }


async def score_policy(
    policy: SchedulerPolicy,
    *,
    shots: ShotSource,
    book_id: str,
    traces: list[ReadingTrace] | None = None,
    base_settings: Settings | None = None,
) -> ArmReport:
    """Replay the trace suite under one policy → its :class:`ArmReport` (zero video)."""
    base = base_settings or get_settings()
    settings = policy.to_settings(base)
    suite = traces or default_trace_suite()
    report = ArmReport(policy=policy.name)
    for trace in suite:
        result = await replay_trace(
            trace,
            shots=shots,
            book_id=book_id,
            settings=settings,
            keyframe_cap=policy.keyframe_cap,
        )
        health = buffer_health(result.samples, low_watermark=result.low)
        report.scores.append(
            TraceScore(
                label=trace.label,
                fraction_above_low=health.fraction_above_low,
                stalls=health.stalls,
                peak_committed_s=max(
                    (s.committed_seconds_ahead for s in result.samples), default=0.0
                ),
                committed_promotions=result.committed_promotions,
                keyframes_ensured=result.keyframes_ensured,
                simulated_earmarks_s=result.simulated_earmarks_s,
                video_seconds_spent=result.video_seconds_spent,
            )
        )
    return report


async def run_ab(
    control: SchedulerPolicy,
    treatment: SchedulerPolicy,
    *,
    shots: ShotSource,
    book_id: str,
    traces: list[ReadingTrace] | None = None,
    base_settings: Settings | None = None,
) -> ABResult:
    """Replay the suite under two policies and report the §13 deltas (zero video)."""
    suite = traces or default_trace_suite()
    control_report = await score_policy(
        control, shots=shots, book_id=book_id, traces=suite, base_settings=base_settings
    )
    treatment_report = await score_policy(
        treatment, shots=shots, book_id=book_id, traces=suite, base_settings=base_settings
    )
    result = ABResult(control=control_report, treatment=treatment_report)
    logger.info("sched.ab", **result.summary())
    return result


__all__ = [
    "ABResult",
    "ArmReport",
    "TraceScore",
    "default_trace_suite",
    "run_ab",
    "score_policy",
]
