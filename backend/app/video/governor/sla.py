"""Per-provider SLA tracking — observed vs target, error-budget burn, a health grade.

The round-1 router has a circuit breaker that reacts to *consecutive* failures.
This is the governance view above it: a rolling, time-windowed picture of how a
provider is doing against its **service-level objectives** so the router/scheduler
can prefer the healthiest backend and an operator can see one *grade* per provider.

What it tracks, over a sliding window of recent outcomes:

* **success rate** vs an SLO target (e.g. 99% of submits succeed),
* **latency** — p50/p95 vs a target p95 ceiling (from a bounded reservoir),
* **error budget** — the SRE primitive: if the SLO is 99% success, the budget is
  the 1% of requests allowed to fail; :class:`SlaTracker` reports the *burn* (what
  fraction of that budget the observed failures have consumed). Burn ≥ 1.0 means
  the budget is exhausted — a breach.

These collapse into a coarse :class:`SlaGrade` (A–F) the oracle exposes and the
router can sort on. All windowing is by *count* of recent samples (a ring), not by
wall-clock, so the grade is deterministic under a fake clock and needs no sweeper;
sample timestamps are recorded only for age-based decay/inspection.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from .clock import Clock, monotonic


class SlaGrade(StrEnum):
    """A coarse, sortable health grade derived from the SLO measurements."""

    A = "A"  # comfortably within SLO
    B = "B"  # within SLO, some headroom eaten
    C = "C"  # error budget burning fast / latency creeping
    D = "D"  # near breach
    F = "F"  # SLO breached (budget exhausted or success below floor)

    @property
    def rank(self) -> int:
        """0 (best, A) … 4 (worst, F) — lower routes first."""
        return "ABCDF".index(self.value)


@dataclass(frozen=True, slots=True)
class SlaObjective:
    """A provider's service-level objectives (targets the tracker grades against)."""

    #: Target fraction of submissions that succeed (e.g. 0.99 = 99%).
    target_success_rate: float = 0.95
    #: Target p95 latency ceiling in milliseconds.
    target_p95_latency_ms: float = 60_000.0
    #: How many recent outcomes the sliding window keeps.
    window_size: int = 100
    #: Minimum samples before a grade is meaningful (below this ⇒ grade B, "unknown
    #: but assume usable" so a fresh provider isn't penalised out of rotation).
    min_samples: int = 5
    #: Error-budget burn fractions that escalate the grade alert (info→warn→crit).
    burn_warning: float = 0.5
    burn_critical: float = 0.9

    def __post_init__(self) -> None:
        if not (0.0 < self.target_success_rate <= 1.0):
            raise ValueError("target_success_rate must be in (0, 1]")
        if self.window_size < 1:
            raise ValueError("window_size must be >= 1")


@dataclass(frozen=True, slots=True)
class SlaSnapshot:
    """A point-in-time SLA picture for one provider."""

    provider: str
    samples: int
    success_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    error_budget_burn: float
    latency_breach: bool
    grade: SlaGrade
    at: float

    @property
    def healthy(self) -> bool:
        """True when the provider is comfortably routable (grade A/B)."""
        return self.grade.rank <= SlaGrade.B.rank

    def as_log_fields(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "samples": self.samples,
            "success_rate": round(self.success_rate, 4),
            "p50_ms": round(self.p50_latency_ms, 1),
            "p95_ms": round(self.p95_latency_ms, 1),
            "error_budget_burn": round(self.error_budget_burn, 4),
            "grade": self.grade.value,
        }


@dataclass
class _Sample:
    ok: bool
    latency_ms: float
    at: float


def _percentile(sorted_values: list[float], pct: float) -> float:
    """The ``pct`` (0..1) percentile of ``sorted_values`` (nearest-rank, simple)."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    rank = pct * (len(sorted_values) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return sorted_values[lo]
    frac = rank - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


class SlaTracker:
    """Rolling SLA bookkeeping for one provider (pure, count-windowed).

    Feed it outcomes with :meth:`record_success` / :meth:`record_failure`; read a
    :class:`SlaSnapshot` with :meth:`snapshot`. The window is a ring of the last
    ``window_size`` outcomes, so memory is bounded and the grade reflects *recent*
    behaviour — a provider that was failing an hour ago but is healthy now grades
    back up as bad samples age out of the ring.
    """

    def __init__(
        self,
        provider: str,
        objective: SlaObjective | None = None,
        *,
        clock: Clock = monotonic,
    ) -> None:
        self.provider = provider
        self.objective = objective or SlaObjective()
        self._clock = clock
        self._samples: deque[_Sample] = deque(maxlen=self.objective.window_size)

    # -- recording -------------------------------------------------------- #

    def record_success(self, latency_ms: float) -> None:
        self._samples.append(_Sample(ok=True, latency_ms=max(0.0, latency_ms), at=self._clock()))

    def record_failure(self, latency_ms: float = 0.0) -> None:
        self._samples.append(_Sample(ok=False, latency_ms=max(0.0, latency_ms), at=self._clock()))

    @property
    def sample_count(self) -> int:
        return len(self._samples)

    # -- measurement ------------------------------------------------------ #

    def _success_rate(self) -> float:
        if not self._samples:
            return 1.0
        ok = sum(1 for s in self._samples if s.ok)
        return ok / len(self._samples)

    def error_budget_burn(self) -> float:
        """Fraction of the allowed error budget consumed by observed failures.

        With SLO success target ``t``, the budget is ``1 - t`` (the failure share
        we tolerate). Observed failure rate ``f`` burns ``f / (1 - t)`` of it. A
        perfect SLO (``t == 1``) has zero budget, so any failure burns ``inf`` →
        clamped to ≥ 1.0 (immediate breach). No samples ⇒ 0 burn.
        """
        if not self._samples:
            return 0.0
        failure_rate = 1.0 - self._success_rate()
        budget = 1.0 - self.objective.target_success_rate
        if budget <= 0:
            return 1.0 if failure_rate > 0 else 0.0
        return failure_rate / budget

    def _latencies(self) -> list[float]:
        return sorted(s.latency_ms for s in self._samples if s.ok)

    def snapshot(self) -> SlaSnapshot:
        """Compute the current :class:`SlaSnapshot` (the graded SLA picture)."""
        n = len(self._samples)
        success = self._success_rate()
        burn = self.error_budget_burn()
        lat = self._latencies()
        p50 = _percentile(lat, 0.50)
        p95 = _percentile(lat, 0.95)
        latency_breach = bool(lat) and p95 > self.objective.target_p95_latency_ms
        grade = self._grade(n=n, success=success, burn=burn, latency_breach=latency_breach)
        return SlaSnapshot(
            provider=self.provider,
            samples=n,
            success_rate=success,
            p50_latency_ms=p50,
            p95_latency_ms=p95,
            error_budget_burn=burn,
            latency_breach=latency_breach,
            grade=grade,
            at=self._clock(),
        )

    def _grade(self, *, n: int, success: float, burn: float, latency_breach: bool) -> SlaGrade:
        """Collapse the measurements into an A–F grade (pure, threshold-based)."""
        if n < self.objective.min_samples:
            # Too few samples to judge — assume usable so a fresh/idle provider is
            # not graded out of rotation, but not A (we haven't earned trust yet).
            return SlaGrade.B
        # A breach dominates: budget exhausted, or success below the SLO floor.
        if burn >= 1.0 or success < self.objective.target_success_rate:
            return SlaGrade.F
        # Latency over the p95 ceiling while still meeting success ⇒ degraded.
        if latency_breach:
            return SlaGrade.C
        if burn >= self.objective.burn_critical:
            return SlaGrade.D
        if burn >= self.objective.burn_warning:
            return SlaGrade.C
        if burn > 0.0:
            return SlaGrade.B
        return SlaGrade.A

    def reset(self) -> None:
        self._samples.clear()


__all__ = [
    "SlaGrade",
    "SlaObjective",
    "SlaSnapshot",
    "SlaTracker",
]
