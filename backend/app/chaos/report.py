"""The game-day findings report — what happened, did the hypothesis hold.

After the runner executes an experiment it emits a :class:`FindingsReport`: the
verdict (hypothesis held / breached / aborted), the timeline of steady-state
samples, the abort reason if any, the injector's call timeline, and a few
derived counters operators care about (how many faults fired, how many calls
were affected, the worst observed margin per bound). It is plain, JSON-able data
— no I/O — so a CLI, an API route, or a test can render or assert on it.

The report is intentionally verbose: a game-day's value is the *evidence*, so we
keep every poll and every injected call rather than a single pass/fail bit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.chaos.interceptor import CallRecord
from app.chaos.steady_state import SteadyStateResult


class Verdict(StrEnum):
    """The top-line outcome of a game-day."""

    #: Hypothesis held for the whole run — the system absorbed the faults.
    HELD = "held"
    #: Steady state breached → auto-abort fired and faults were rolled back.
    BREACHED = "breached"
    #: Halted early by an abort *condition* (error/time cap) without a breach.
    ABORTED = "aborted"
    #: Refused to start — the system was already unhealthy before any fault.
    PREFLIGHT_FAILED = "preflight_failed"


@dataclass(frozen=True, slots=True)
class SteadyStateSample:
    """One poll of the steady-state guard during the run."""

    monotonic_at: float
    result: SteadyStateResult
    armed_dependencies: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "at": self.monotonic_at,
            "held": self.result.held,
            "armed": list(self.armed_dependencies),
            "steady_state": self.result.to_dict(),
        }


@dataclass(slots=True)
class FindingsReport:
    """The full record of a single game-day run."""

    experiment_name: str
    verdict: Verdict
    started_at: float
    ended_at: float
    samples: list[SteadyStateSample] = field(default_factory=list)
    call_timeline: list[CallRecord] = field(default_factory=list)
    abort_reason: str | None = None
    #: The bounds that were breaching at the moment of abort (if any).
    breaching_metrics: list[str] = field(default_factory=list)
    seed: int = 0

    @property
    def held(self) -> bool:
        return self.verdict is Verdict.HELD

    @property
    def duration_s(self) -> float:
        return self.ended_at - self.started_at

    @property
    def faults_fired(self) -> int:
        """How many injected calls actually fired a fault (delay/raise/mutate)."""
        return sum(1 for c in self.call_timeline if c.fault_name is not None)

    @property
    def calls_raised(self) -> int:
        """How many injected calls raised an error."""
        return sum(1 for c in self.call_timeline if c.raised is not None)

    @property
    def affected_dependencies(self) -> set[str]:
        return {c.dependency for c in self.call_timeline if c.fault_name is not None}

    def worst_margins(self) -> dict[str, float]:
        """The minimum (worst) margin seen per metric across all samples.

        A negative value means the bound breached at some point; the magnitude is
        how far past the threshold the system got — the headline resilience number.
        """
        worst: dict[str, float] = {}
        for sample in self.samples:
            for bound_result in sample.result.results:
                metric = bound_result.bound.metric
                margin = bound_result.margin
                if metric not in worst or margin < worst[metric]:
                    worst[metric] = margin
        return worst

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment": self.experiment_name,
            "verdict": self.verdict.value,
            "held": self.held,
            "seed": self.seed,
            "duration_s": self.duration_s,
            "abort_reason": self.abort_reason,
            "breaching_metrics": list(self.breaching_metrics),
            "counters": {
                "polls": len(self.samples),
                "injected_calls": len(self.call_timeline),
                "faults_fired": self.faults_fired,
                "calls_raised": self.calls_raised,
                "affected_dependencies": sorted(self.affected_dependencies),
            },
            "worst_margins": self.worst_margins(),
            "samples": [s.to_dict() for s in self.samples],
            "call_timeline": [
                {
                    "dependency": c.dependency,
                    "call_index": c.call_index,
                    "fault": c.fault_name,
                    "effect": c.effect_label,
                    "delay_s": c.delay_s,
                    "raised": c.raised,
                    "at": c.monotonic_at,
                }
                for c in self.call_timeline
            ],
        }

    def summary_line(self) -> str:
        """A one-line human summary for a log / CLI footer."""
        head = f"[chaos] {self.experiment_name}: {self.verdict.value.upper()}"
        if self.abort_reason:
            head += f" — {self.abort_reason}"
        return (
            f"{head} | {len(self.samples)} polls, {self.faults_fired} faults fired, "
            f"{self.calls_raised} calls raised, {self.duration_s:.1f}s"
        )


__all__ = ["FindingsReport", "SteadyStateSample", "Verdict"]
