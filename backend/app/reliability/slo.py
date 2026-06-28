"""SLOs, error budgets, and burn-rate alerting (kinora.md §12.5).

An SLO is a target on a service-level *indicator* (SLI) — availability, or a
latency percentile — over a window. The **error budget** is the slack the SLO
allows (``1 - target`` of the requests may miss); **burn rate** is how fast a
period is consuming that budget relative to spending it evenly. The Google-SRE
multi-window burn-rate rule turns a continuous SLI into a discrete page/ticket
signal, which is what the canary + load report gate on.

This is pure arithmetic over already-measured numbers (an availability fraction,
a measured percentile, a budget window). The :class:`SLOSet` evaluates a whole
report at once and yields a pass/fail verdict with the offending SLOs, which the
load runner uses as its CLI exit code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class SLIKind(StrEnum):
    """The kind of indicator an SLO targets."""

    AVAILABILITY = "availability"  # success fraction (higher is better)
    LATENCY_P50 = "latency_p50_ms"
    LATENCY_P90 = "latency_p90_ms"
    LATENCY_P95 = "latency_p95_ms"
    LATENCY_P99 = "latency_p99_ms"
    LATENCY_P999 = "latency_p999_ms"
    ERROR_RATE = "error_rate"  # failure fraction (lower is better)


#: Latency SLIs and the error-rate SLI are "upper-bound" objectives (measured <=
#: target); availability is a "lower-bound" objective (measured >= target).
_LOWER_BOUND = {SLIKind.AVAILABILITY}


@dataclass(frozen=True, slots=True)
class SLO:
    """One service-level objective: an SLI must stay on the right side of a target.

    ``endpoint`` scopes the objective to one report bucket (``None`` = the
    aggregate). For availability the measured value must be ``>= target``; for
    latency / error-rate it must be ``<= target``.
    """

    name: str
    kind: SLIKind
    target: float
    endpoint: str | None = None

    @property
    def is_lower_bound(self) -> bool:
        """True when the SLI must be *at least* the target (availability)."""
        return self.kind in _LOWER_BOUND

    def evaluate(self, measured: float) -> SLOResult:
        """Check a measured SLI value against this objective."""
        if self.is_lower_bound:
            met = measured >= self.target
            margin = measured - self.target
        else:
            met = measured <= self.target
            margin = self.target - measured
        return SLOResult(slo=self, measured=measured, met=met, margin=margin)


@dataclass(frozen=True, slots=True)
class SLOResult:
    """The verdict for one SLO against a measured value."""

    slo: SLO
    measured: float
    met: bool
    #: Signed slack: ``>= 0`` means the objective was met by this margin.
    margin: float

    def to_dict(self) -> dict[str, object]:
        """JSON projection of the verdict."""
        return {
            "name": self.slo.name,
            "kind": self.slo.kind.value,
            "endpoint": self.slo.endpoint,
            "target": self.slo.target,
            "measured": round(self.measured, 4),
            "met": self.met,
            "margin": round(self.margin, 4),
        }


@dataclass(frozen=True, slots=True)
class SLOVerdict:
    """The aggregate pass/fail of an :class:`SLOSet` over a report."""

    passed: bool
    results: tuple[SLOResult, ...]

    @property
    def violations(self) -> list[SLOResult]:
        """The SLOs that were not met (the ones to alert on)."""
        return [r for r in self.results if not r.met]

    def to_dict(self) -> dict[str, object]:
        """JSON projection of the verdict (passed + per-SLO results)."""
        return {
            "passed": self.passed,
            "results": [r.to_dict() for r in self.results],
        }

    def render_text(self) -> str:
        """A compact pass/fail summary (one line per SLO)."""
        lines = [f"SLO verdict: {'PASS' if self.passed else 'FAIL'}"]
        for r in self.results:
            mark = "ok " if r.met else "MISS"
            scope = r.slo.endpoint or "(aggregate)"
            lines.append(
                f"  [{mark}] {r.slo.name:<26} {scope:<30} "
                f"measured={r.measured:.3f} target={r.slo.target:.3f}"
            )
        return "\n".join(lines)


def _measure(report: object, slo: SLO) -> float:
    """Extract the SLI value ``slo`` targets from a LoadReport-like object."""
    # Imported lazily to avoid a hard import cycle in type-only contexts.
    from app.reliability.metrics_report import LoadReport

    assert isinstance(report, LoadReport)
    if slo.endpoint is not None:
        stats = report.endpoints.get(slo.endpoint)
        if stats is None:
            # An endpoint that received no traffic vacuously meets latency SLOs
            # (0) and a perfect availability (1.0) / zero error-rate.
            if slo.kind is SLIKind.AVAILABILITY:
                return 1.0
            if slo.kind is SLIKind.ERROR_RATE:
                return 0.0
            return 0.0
        if slo.kind is SLIKind.AVAILABILITY:
            return 1.0 - stats.error_rate
        if slo.kind is SLIKind.ERROR_RATE:
            return stats.error_rate
        summary = stats.latency()
        return _latency_field(summary, slo.kind)
    # Aggregate scope.
    if slo.kind is SLIKind.AVAILABILITY:
        return report.availability
    if slo.kind is SLIKind.ERROR_RATE:
        return report.error_rate
    return _latency_field(report.overall_latency(), slo.kind)


def _latency_field(summary: object, kind: SLIKind) -> float:
    from app.reliability.latency import LatencySummary

    assert isinstance(summary, LatencySummary)
    mapping = {
        SLIKind.LATENCY_P50: summary.p50_ms,
        SLIKind.LATENCY_P90: summary.p90_ms,
        SLIKind.LATENCY_P95: summary.p95_ms,
        SLIKind.LATENCY_P99: summary.p99_ms,
        SLIKind.LATENCY_P999: summary.p999_ms,
    }
    return mapping[kind]


@dataclass(frozen=True, slots=True)
class SLOSet:
    """A bundle of SLOs evaluated together against a load report."""

    slos: tuple[SLO, ...]

    def evaluate_report(self, report: object) -> SLOVerdict:
        """Evaluate every SLO against ``report`` (a :class:`LoadReport`)."""
        results = tuple(slo.evaluate(_measure(report, slo)) for slo in self.slos)
        return SLOVerdict(passed=all(r.met for r in results), results=results)


# --------------------------------------------------------------------------- #
# Error budget + burn rate (the alerting math)
# --------------------------------------------------------------------------- #


def error_budget(target_availability: float) -> float:
    """The fraction of requests allowed to fail under an availability SLO.

    e.g. a 99.5% availability target permits a 0.5% error budget.
    """
    if not 0.0 <= target_availability <= 1.0:
        raise ValueError("target_availability must be in [0, 1]")
    return 1.0 - target_availability


def burn_rate(observed_error_rate: float, target_availability: float) -> float:
    """How fast the error budget is being consumed (``1.0`` == exactly on budget).

    A burn rate > 1 means the budget is being spent faster than the window
    allows; a burn rate of 14.4 over 1h is the classic SRE fast-burn page
    threshold (it would exhaust a 30-day 99.9% budget in ~2 days).
    """
    budget = error_budget(target_availability)
    if budget <= 0.0:
        # A 100%-availability SLO has no budget; any error is an infinite burn.
        return 0.0 if observed_error_rate <= 0.0 else float("inf")
    return observed_error_rate / budget


@dataclass(frozen=True, slots=True)
class BurnRateWindow:
    """One window of a multi-window burn-rate alert (Google SRE)."""

    window_label: str
    observed_error_rate: float
    threshold: float  # burn-rate threshold that fires this window

    def fires(self, target_availability: float) -> bool:
        """Whether this window's burn rate crosses its alert threshold."""
        return burn_rate(self.observed_error_rate, target_availability) >= self.threshold


@dataclass(frozen=True, slots=True)
class MultiWindowBurnAlert:
    """A multi-window burn-rate alert: it fires only when *all* windows agree.

    Requiring agreement across a fast window and a slow window suppresses both
    one-off spikes (fast fires, slow doesn't) and slow drifts that have already
    self-healed (slow fires, fast doesn't) — the standard SRE noise filter.
    """

    target_availability: float
    windows: Sequence[BurnRateWindow] = field(default_factory=tuple)

    def fires(self) -> bool:
        """True only when every configured window crosses its threshold."""
        return bool(self.windows) and all(
            w.fires(self.target_availability) for w in self.windows
        )


# --------------------------------------------------------------------------- #
# Default Kinora SLO set (the §12.5 demo gate)
# --------------------------------------------------------------------------- #


def default_kinora_slos(
    *,
    intent_p99_ms: float = 250.0,
    seek_p99_ms: float = 150.0,
    availability: float = 0.995,
) -> SLOSet:
    """The standard Kinora generation-on-scroll SLO set (§4/§12.5).

    The intent endpoint is the §4.9 control tick (must stay snappy so the buffer
    keeps up); seek must bridge ≈instantly (§4.8 latency-to-first-frame); overall
    availability must clear the target. Defaults are wired from
    ``Settings.slo_*`` by the CLI.
    """
    from app.reliability.scenarios import EP_INTENT, EP_SEEK

    return SLOSet(
        slos=(
            SLO("intent-p99", SLIKind.LATENCY_P99, intent_p99_ms, endpoint=EP_INTENT),
            SLO("seek-p99", SLIKind.LATENCY_P99, seek_p99_ms, endpoint=EP_SEEK),
            SLO("availability", SLIKind.AVAILABILITY, availability, endpoint=None),
        )
    )


def slos_from_settings(settings: object) -> SLOSet:
    """Build the Kinora SLO set from application :class:`Settings` (additive cfg).

    Reads the ``slo_intent_p99_ms`` / ``slo_seek_coherent_p99_ms`` /
    ``slo_availability_target`` fields the reliability toolkit added to the config,
    so a deployment can tune the gate without code changes.
    """
    return default_kinora_slos(
        intent_p99_ms=float(getattr(settings, "slo_intent_p99_ms", 250.0)),
        seek_p99_ms=float(getattr(settings, "slo_seek_coherent_p99_ms", 150.0)),
        availability=float(getattr(settings, "slo_availability_target", 0.995)),
    )


__all__ = [
    "BurnRateWindow",
    "MultiWindowBurnAlert",
    "SLIKind",
    "SLO",
    "SLOResult",
    "SLOSet",
    "SLOVerdict",
    "burn_rate",
    "default_kinora_slos",
    "error_budget",
    "slos_from_settings",
]
