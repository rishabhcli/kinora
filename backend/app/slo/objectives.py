"""SLO definitions, error-budget accounting, and multi-window burn-rate alerts.

This is the SRE-workbook math for *availability-style* (ratio) SLOs computed
from a **live** rolling stream (§12.5):

* An :class:`Objective` binds an SLI to a target over a 30-day budget window.
* The **error budget** is the slack ``1 - target`` of events allowed to fail
  over the window; :class:`BudgetState` reports the budget consumed / remaining
  and a percentage.
* **Burn rate** is how fast the *current* failure rate is consuming that budget
  relative to spending it evenly — burn-rate 1.0 exactly exhausts the budget at
  the window's end; 14.4 exhausts a 30-day budget in ~2 days.
* A :class:`MultiWindowBurnPolicy` is the canonical fast/slow two-window-pair
  alert: a **fast-burn** page (high burn over a short window, confirmed by a
  medium window) and a **slow-burn** ticket (lower burn over a long window,
  confirmed by a medium window). Each pair fires only when *both* its windows
  agree, which suppresses one-off spikes and already-healed drifts.

Latency SLOs use a simpler threshold compare (:class:`LatencyObjective`) — a
percentile must stay ``<= target`` — since "error budget" is a request-accounting
notion that only applies cleanly to the ratio SLIs.

Distinct from ``app.reliability.slo`` (which evaluates a finished load report);
this operates continuously on the live :mod:`app.slo.windows` streams.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from app.slo.sli import SLIDefinition, SLIType, SLIValue


class AlertSeverity(StrEnum):
    """How loud a burn-rate alert is."""

    NONE = "none"
    TICKET = "ticket"  # slow burn — open a ticket
    PAGE = "page"  # fast burn — page someone now


# --------------------------------------------------------------------------- #
# Ratio-SLI objectives + error budget
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Objective:
    """An availability-style SLO on a ratio SLI over a budget window.

    ``target`` is the fraction that must be good (e.g. ``0.995``). ``window_s``
    is the error-budget accounting window (default 30 days). The SLI must be a
    :class:`SLIType.RATIO_GOOD` indicator.
    """

    name: str
    sli: SLIDefinition
    target: float
    window_s: float = 30 * 24 * 3600.0

    def __post_init__(self) -> None:
        if not 0.0 <= self.target <= 1.0:
            raise ValueError("target must be in [0, 1]")
        if self.sli.type is not SLIType.RATIO_GOOD:
            raise ValueError(f"Objective {self.name!r} needs a RATIO_GOOD SLI")

    @property
    def error_budget(self) -> float:
        """Fraction of events permitted to fail under the target (``1 - target``)."""
        return 1.0 - self.target


@dataclass(frozen=True, slots=True)
class BudgetState:
    """The error-budget accounting for an :class:`Objective` over a window.

    Computed from the *long-window* SLI value: ``good_ratio`` is the achieved
    fraction good; ``failure_ratio = 1 - good_ratio`` is what's been spent.
    """

    objective: Objective
    good_ratio: float
    sample_count: int

    @property
    def failure_ratio(self) -> float:
        return 1.0 - self.good_ratio

    @property
    def budget(self) -> float:
        """The total error budget (= the objective's ``1 - target``)."""
        return self.objective.error_budget

    @property
    def consumed(self) -> float:
        """The fraction of the *budget* that's been spent (clamped to ``[0, ∞)``).

        ``failure_ratio / budget``: 0 means none spent, 1.0 means exactly the
        budget, > 1 means the SLO is already blown for the window.
        """
        if self.budget <= 0.0:
            return 0.0 if self.failure_ratio <= 0.0 else float("inf")
        return self.failure_ratio / self.budget

    @property
    def remaining_fraction(self) -> float:
        """Budget remaining as a fraction in ``(-∞, 1]`` (1.0 = full, <0 = blown)."""
        return 1.0 - self.consumed

    @property
    def is_exhausted(self) -> bool:
        """True when the error budget for the window is fully spent or overspent."""
        return self.remaining_fraction <= 0.0

    @property
    def met(self) -> bool:
        """True when the achieved good-ratio still clears the target."""
        return self.good_ratio >= self.objective.target

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective.name,
            "target": self.objective.target,
            "good_ratio": round(self.good_ratio, 6),
            "budget": round(self.budget, 6),
            "consumed_fraction": round(self.consumed, 6)
            if self.consumed != float("inf")
            else "inf",
            "remaining_fraction": round(self.remaining_fraction, 6)
            if self.remaining_fraction != float("-inf")
            else "-inf",
            "remaining_pct": round(max(self.remaining_fraction, 0.0) * 100.0, 3),
            "exhausted": self.is_exhausted,
            "met": self.met,
            "sample_count": self.sample_count,
        }


def burn_rate(failure_ratio: float, error_budget: float) -> float:
    """How fast the budget is being consumed (1.0 == exactly on budget).

    ``failure_ratio`` is the *windowed* failure fraction; ``error_budget`` is
    ``1 - target``. A 100%-target SLO has no budget: any failure is an infinite
    burn, zero failure is zero burn.
    """
    if error_budget <= 0.0:
        return 0.0 if failure_ratio <= 0.0 else float("inf")
    return failure_ratio / error_budget


@dataclass(frozen=True, slots=True)
class BurnWindow:
    """One window of a burn-rate condition (a short or a long lookback)."""

    label: str
    window_s: float
    threshold: float  # burn-rate value at/above which this window trips

    def trips(self, failure_ratio: float, error_budget: float) -> bool:
        # A burn rate exactly *at* the threshold trips. A tiny relative epsilon
        # absorbs float noise (e.g. 0.072 / 0.005 == 14.399999…, not 14.4) so an
        # on-the-nose budget burn isn't silently dropped below its own threshold.
        rate = burn_rate(failure_ratio, error_budget)
        return rate >= self.threshold * (1.0 - 1e-9)


@dataclass(frozen=True, slots=True)
class BurnCondition:
    """A fast/slow burn condition: a (long, short) window pair + a severity.

    Fires only when **both** windows trip — the long window establishes the
    sustained trend and the short window confirms it's still happening now.
    """

    severity: AlertSeverity
    long_window: BurnWindow
    short_window: BurnWindow

    def fires(self, *, long_failure: float, short_failure: float, error_budget: float) -> bool:
        return self.long_window.trips(long_failure, error_budget) and self.short_window.trips(
            short_failure, error_budget
        )


@dataclass(frozen=True, slots=True)
class BurnAlert:
    """The evaluated burn-rate verdict for one objective."""

    objective_name: str
    severity: AlertSeverity
    long_burn: float
    short_burn: float
    detail: str

    @property
    def firing(self) -> bool:
        return self.severity is not AlertSeverity.NONE

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective_name,
            "severity": self.severity.value,
            "firing": self.firing,
            "long_burn": _round_inf(self.long_burn),
            "short_burn": _round_inf(self.short_burn),
            "detail": self.detail,
        }


def _round_inf(x: float) -> object:
    if x == float("inf"):
        return "inf"
    return round(x, 4)


@dataclass(frozen=True, slots=True)
class MultiWindowBurnPolicy:
    """The SRE multi-window burn-rate alerting policy (ordered by severity).

    Conditions are evaluated worst-first; the first that fires wins, so a
    fast-burn PAGE pre-empts a slow-burn TICKET. The default policy mirrors the
    SRE-workbook 30-day recommendation:

    * **PAGE** — 14.4x burn over the last hour, confirmed by the last 5 min
      (would exhaust the whole budget in ~2 days).
    * **TICKET** — 1x burn over the last 3 days, confirmed by the last 6 hours
      (a slow drift that will exhaust the budget by window's end).
    """

    conditions: tuple[BurnCondition, ...]

    def evaluate(
        self,
        objective: Objective,
        *,
        failure_by_window: dict[float, float],
    ) -> BurnAlert:
        """Pick the highest-severity condition that fires for ``objective``.

        ``failure_by_window`` maps a window length (s) to the failure ratio
        measured over it; every window referenced by a condition must be present.
        """
        eb = objective.error_budget
        worst: BurnAlert | None = None
        for cond in self.conditions:
            lw = failure_by_window[cond.long_window.window_s]
            sw = failure_by_window[cond.short_window.window_s]
            if cond.fires(long_failure=lw, short_failure=sw, error_budget=eb):
                alert = BurnAlert(
                    objective_name=objective.name,
                    severity=cond.severity,
                    long_burn=burn_rate(lw, eb),
                    short_burn=burn_rate(sw, eb),
                    detail=(
                        f"{cond.severity.value}: burn over {cond.long_window.label} "
                        f"({burn_rate(lw, eb):.2f}x) and {cond.short_window.label} "
                        f"({burn_rate(sw, eb):.2f}x) crossed thresholds"
                    ),
                )
                # Conditions are sorted worst-first below, but be defensive.
                if worst is None or _SEV_RANK[alert.severity] > _SEV_RANK[worst.severity]:
                    worst = alert
        if worst is not None:
            return worst
        # Nothing fired — report the largest observed burn for context.
        biggest = max(failure_by_window.values(), default=0.0)
        return BurnAlert(
            objective_name=objective.name,
            severity=AlertSeverity.NONE,
            long_burn=burn_rate(biggest, eb),
            short_burn=burn_rate(biggest, eb),
            detail="no burn-rate condition firing",
        )

    @property
    def window_lengths(self) -> tuple[float, ...]:
        """Every distinct window length the policy needs measured (for the engine)."""
        out: set[float] = set()
        for c in self.conditions:
            out.add(c.long_window.window_s)
            out.add(c.short_window.window_s)
        return tuple(sorted(out))


_SEV_RANK = {AlertSeverity.NONE: 0, AlertSeverity.TICKET: 1, AlertSeverity.PAGE: 2}


def default_burn_policy() -> MultiWindowBurnPolicy:
    """The standard 30-day fast/slow multi-window burn policy (SRE workbook)."""
    return MultiWindowBurnPolicy(
        conditions=(
            BurnCondition(
                severity=AlertSeverity.PAGE,
                long_window=BurnWindow("1h", 3600.0, 14.4),
                short_window=BurnWindow("5m", 300.0, 14.4),
            ),
            BurnCondition(
                severity=AlertSeverity.TICKET,
                long_window=BurnWindow("3d", 3 * 24 * 3600.0, 1.0),
                short_window=BurnWindow("6h", 6 * 3600.0, 1.0),
            ),
        ),
    )


# --------------------------------------------------------------------------- #
# Latency objectives (threshold compare; no error budget)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LatencyObjective:
    """A latency SLO: a percentile SLI must stay ``<= target_ms`` over a window."""

    name: str
    sli: SLIDefinition
    target_ms: float
    window_s: float = 3600.0

    def __post_init__(self) -> None:
        if self.sli.type is SLIType.RATIO_GOOD:
            raise ValueError(f"LatencyObjective {self.name!r} needs a latency SLI")

    def evaluate(self, value: SLIValue) -> LatencyVerdict:
        met = value.value <= self.target_ms
        return LatencyVerdict(
            objective=self,
            measured_ms=value.value,
            met=met,
            margin_ms=self.target_ms - value.value,
            sample_count=value.sample_count,
            empty=value.empty,
        )


@dataclass(frozen=True, slots=True)
class LatencyVerdict:
    objective: LatencyObjective
    measured_ms: float
    met: bool
    margin_ms: float
    sample_count: int
    empty: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective.name,
            "target_ms": self.objective.target_ms,
            "measured_ms": round(self.measured_ms, 4),
            "met": self.met,
            "margin_ms": round(self.margin_ms, 4),
            "sample_count": self.sample_count,
            "empty": self.empty,
        }


__all__ = [
    "AlertSeverity",
    "BudgetState",
    "BurnAlert",
    "BurnCondition",
    "BurnWindow",
    "LatencyObjective",
    "LatencyVerdict",
    "MultiWindowBurnPolicy",
    "Objective",
    "burn_rate",
    "default_burn_policy",
]
