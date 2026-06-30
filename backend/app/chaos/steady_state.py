"""Steady-state hypothesis + SLO guard (Chaos-Engineering "steady state").

A chaos experiment is framed as a *hypothesis*: "the system stays in its steady
state even while this fault is active". The steady state is defined by one or
more measurable :class:`SteadyStateBound` checks — availability stays above a
floor, p99 latency stays below a ceiling, error rate stays under a cap. Before
arming any fault the runner samples the steady state to confirm the system is
healthy to begin with (no point breaking an already-broken system); during the
experiment it polls the guard, and the first poll that **breaches** trips the
auto-abort.

This module is pure arithmetic over an already-measured snapshot. The runner
supplies the snapshot via a probe (an async callable the *caller* provides — it
is the thing under test, e.g. "drive a reader session and measure"). The guard
turns a snapshot into a pass/fail verdict with the offending bounds named.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Comparison(StrEnum):
    """Which side of the threshold a metric must stay on to be healthy."""

    AT_LEAST = "at_least"  # measured >= threshold (availability)
    AT_MOST = "at_most"  # measured <= threshold (latency, error rate)


@dataclass(frozen=True, slots=True)
class SteadyStateBound:
    """One measurable invariant of the steady state.

    ``metric`` keys into the probe's snapshot mapping. ``comparison`` says
    whether bigger or smaller is healthy; ``threshold`` is the boundary.
    """

    metric: str
    comparison: Comparison
    threshold: float

    def check(self, value: float) -> BoundResult:
        """Evaluate one measured value against this bound."""
        if self.comparison is Comparison.AT_LEAST:
            ok = value >= self.threshold
            margin = value - self.threshold
        else:
            ok = value <= self.threshold
            margin = self.threshold - value
        return BoundResult(bound=self, measured=value, ok=ok, margin=margin)


@dataclass(frozen=True, slots=True)
class BoundResult:
    """The verdict for one bound against a measured value."""

    bound: SteadyStateBound
    measured: float
    ok: bool
    #: Signed slack: ``>= 0`` means the bound held by this margin.
    margin: float

    def to_dict(self) -> dict[str, object]:
        return {
            "metric": self.bound.metric,
            "comparison": self.bound.comparison.value,
            "threshold": self.bound.threshold,
            "measured": self.measured,
            "ok": self.ok,
            "margin": self.margin,
        }


@dataclass(frozen=True, slots=True)
class SteadyStateResult:
    """The verdict for a whole steady-state snapshot (all bounds)."""

    results: tuple[BoundResult, ...]

    @property
    def held(self) -> bool:
        """True only when *every* bound held."""
        return all(r.ok for r in self.results)

    @property
    def breached(self) -> tuple[BoundResult, ...]:
        """The bounds that failed (empty when the steady state held)."""
        return tuple(r for r in self.results if not r.ok)

    def to_dict(self) -> dict[str, object]:
        return {
            "held": self.held,
            "bounds": [r.to_dict() for r in self.results],
            "breached": [r.bound.metric for r in self.breached],
        }


@dataclass(frozen=True, slots=True)
class SteadyStateHypothesis:
    """The set of bounds that together define "the system is healthy".

    A snapshot missing a bound's ``metric`` is treated as a breach of that bound
    (a metric you cannot measure is, conservatively, not known-healthy).
    """

    bounds: tuple[SteadyStateBound, ...]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.bounds:
            raise ValueError("a steady-state hypothesis needs at least one bound")

    def evaluate(self, snapshot: Mapping[str, float]) -> SteadyStateResult:
        """Turn a measured snapshot into a pass/fail verdict over all bounds."""
        results: list[BoundResult] = []
        for bound in self.bounds:
            if bound.metric not in snapshot:
                # Unmeasurable metric → conservative breach (margin is -inf-ish).
                results.append(
                    BoundResult(
                        bound=bound, measured=float("nan"), ok=False, margin=float("-inf")
                    )
                )
            else:
                results.append(bound.check(float(snapshot[bound.metric])))
        return SteadyStateResult(results=tuple(results))

    @staticmethod
    def of(bounds: Sequence[SteadyStateBound], description: str = "") -> SteadyStateHypothesis:
        """Convenience constructor from any sequence of bounds."""
        return SteadyStateHypothesis(bounds=tuple(bounds), description=description)


# -- ergonomic bound builders -----------------------------------------------


def availability_at_least(fraction: float, metric: str = "availability") -> SteadyStateBound:
    """Availability fraction must stay ``>= fraction`` (e.g. 0.99)."""
    return SteadyStateBound(metric=metric, comparison=Comparison.AT_LEAST, threshold=fraction)


def error_rate_at_most(fraction: float, metric: str = "error_rate") -> SteadyStateBound:
    """Error-rate fraction must stay ``<= fraction`` (e.g. 0.01)."""
    return SteadyStateBound(metric=metric, comparison=Comparison.AT_MOST, threshold=fraction)


def latency_at_most(ms: float, metric: str = "p99_latency_ms") -> SteadyStateBound:
    """A latency percentile (ms) must stay ``<= ms``."""
    return SteadyStateBound(metric=metric, comparison=Comparison.AT_MOST, threshold=ms)


__all__ = [
    "BoundResult",
    "Comparison",
    "SteadyStateBound",
    "SteadyStateHypothesis",
    "SteadyStateResult",
    "availability_at_least",
    "error_rate_at_most",
    "latency_at_most",
]
