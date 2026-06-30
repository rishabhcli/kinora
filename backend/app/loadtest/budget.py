"""Per-endpoint latency budgets + a pass/fail gate (the SLO contract, §12.5).

A load run is only useful if it produces a verdict. A :class:`LatencyBudget`
names, *per logical endpoint*, the percentile thresholds a healthy run must stay
under (e.g. ``page_turn`` p95 ≤ 250 ms, p99 ≤ 600 ms), plus an error-rate ceiling
and an optional minimum throughput. :func:`evaluate_budget` checks a finished
:class:`~app.loadtest.collector.LatencyCollector` against the budget and returns a
structured :class:`GateResult` whose ``passed`` flag is what a CI job keys off.

Budgets are expressed against the **omission-corrected** percentiles by default —
gating on the naive service latency would pass a run that actually queued readers
behind a stall. The default Kinora budgets reflect §4: reading is slow, so the
*control-plane* reads (open book, buffer poll, page turn) must feel instant
(sub-300 ms p95) while the heavier write paths (jump re-promotion, director
comment → regen trigger) get more headroom because they kick off background work
rather than block the reader.

Pure and synchronous; the unit tests assert pass and fail on crafted collectors.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from app.loadtest.collector import LatencyCollector
from app.loadtest.histogram import LatencySummary


class LatencyBasis(StrEnum):
    """Which latency a budget is checked against."""

    CORRECTED = "corrected"  # omission-corrected user-perceived latency (default)
    SERVICE = "service"  # raw service latency the target reported


@dataclass(frozen=True, slots=True)
class EndpointBudget:
    """The threshold contract for one logical endpoint (seconds)."""

    endpoint: str
    #: percentile → max allowed latency (seconds). e.g. {"p95": 0.25, "p99": 0.6}.
    max_latency_s: Mapping[str, float] = field(default_factory=dict)
    #: Max fraction of non-OK outcomes tolerated (0..1).
    max_error_rate: float = 0.01
    #: Optional floor on completed throughput for this endpoint (req/s).
    min_throughput_rps: float | None = None

    def __post_init__(self) -> None:
        for key in self.max_latency_s:
            if key not in _PERCENTILE_ATTR:
                raise ValueError(
                    f"unknown percentile key {key!r}; use one of {sorted(_PERCENTILE_ATTR)}"
                )


@dataclass(frozen=True, slots=True)
class LatencyBudget:
    """A whole-run budget: per-endpoint contracts + an optional aggregate one."""

    endpoints: Mapping[str, EndpointBudget] = field(default_factory=dict)
    #: Optional aggregate contract checked against the merged latencies.
    aggregate: EndpointBudget | None = None
    basis: LatencyBasis = LatencyBasis.CORRECTED


#: percentile key → attribute on :class:`LatencySummary`.
_PERCENTILE_ATTR: dict[str, str] = {
    "p50": "p50",
    "p90": "p90",
    "p95": "p95",
    "p99": "p99",
    "p999": "p999",
    "max": "max",
    "mean": "mean",
}


@dataclass(frozen=True, slots=True)
class Violation:
    """A single failed assertion within the gate."""

    endpoint: str
    metric: str  # e.g. "p95", "error_rate", "throughput"
    observed: float
    threshold: float

    def message(self) -> str:
        return (
            f"{self.endpoint}: {self.metric}={self.observed:.4g} "
            f"exceeds budget {self.threshold:.4g}"
        )


@dataclass(frozen=True, slots=True)
class GateResult:
    """The verdict of a budget evaluation."""

    passed: bool
    violations: Sequence[Violation]
    #: endpoints that had a budget but no recorded samples (informational).
    missing_endpoints: Sequence[str] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "violations": [
                {
                    "endpoint": v.endpoint,
                    "metric": v.metric,
                    "observed": v.observed,
                    "threshold": v.threshold,
                }
                for v in self.violations
            ],
            "missing_endpoints": list(self.missing_endpoints),
        }

    def summary_line(self) -> str:
        if self.passed:
            return "PASS — all latency / error / throughput budgets met"
        return f"FAIL — {len(self.violations)} budget violation(s)"


def _summary_value(summary: LatencySummary, metric: str) -> float:
    return float(getattr(summary, _PERCENTILE_ATTR[metric]))


def _check_endpoint(
    budget: EndpointBudget,
    summary: LatencySummary,
    *,
    error_rate: float,
    throughput_rps: float,
) -> list[Violation]:
    violations: list[Violation] = []
    for metric, threshold in budget.max_latency_s.items():
        observed = _summary_value(summary, metric)
        if observed > threshold:
            violations.append(Violation(budget.endpoint, metric, observed, threshold))
    if error_rate > budget.max_error_rate:
        violations.append(
            Violation(budget.endpoint, "error_rate", error_rate, budget.max_error_rate)
        )
    if budget.min_throughput_rps is not None and throughput_rps < budget.min_throughput_rps:
        violations.append(
            Violation(
                budget.endpoint,
                "throughput",
                throughput_rps,
                budget.min_throughput_rps,
            )
        )
    return violations


def evaluate_budget(collector: LatencyCollector, budget: LatencyBudget) -> GateResult:
    """Check a finished collector against ``budget``; return the pass/fail verdict."""
    violations: list[Violation] = []
    missing: list[str] = []
    use_corrected = budget.basis is LatencyBasis.CORRECTED

    for endpoint, eb in budget.endpoints.items():
        try:
            st = collector.stats_for(endpoint)
        except KeyError:
            missing.append(endpoint)
            continue
        summary = (st.corrected if use_corrected else st.service).summary()
        violations.extend(
            _check_endpoint(
                eb,
                summary,
                error_rate=st.counts.error_rate,
                throughput_rps=collector.throughput_rps(endpoint=endpoint),
            )
        )

    if budget.aggregate is not None:
        agg = collector.aggregate()
        summary = (agg.corrected if use_corrected else agg.service).summary()
        violations.extend(
            _check_endpoint(
                budget.aggregate,
                summary,
                error_rate=agg.counts.error_rate,
                throughput_rps=collector.throughput_rps(),
            )
        )

    return GateResult(
        passed=not violations,
        violations=tuple(violations),
        missing_endpoints=tuple(missing),
    )


def default_kinora_budget() -> LatencyBudget:
    """The shipped Kinora reading-plane budget (§4 control-plane latencies).

    Control-plane reads must feel instant; the write paths that spawn background
    work get headroom. Tune per environment; this is a sane CI default.
    """
    return LatencyBudget(
        endpoints={
            "open_book": EndpointBudget(
                "open_book", {"p95": 0.5, "p99": 1.0}, max_error_rate=0.005
            ),
            "buffer_state": EndpointBudget(
                "buffer_state", {"p95": 0.15, "p99": 0.3}, max_error_rate=0.01
            ),
            "page_turn": EndpointBudget(
                "page_turn", {"p95": 0.25, "p99": 0.6}, max_error_rate=0.01
            ),
            "jump": EndpointBudget("jump", {"p95": 0.4, "p99": 0.9}, max_error_rate=0.02),
            "comment": EndpointBudget(
                "comment", {"p95": 0.8, "p99": 1.5}, max_error_rate=0.02
            ),
        },
        aggregate=EndpointBudget("__all__", {"p99": 1.5}, max_error_rate=0.02),
        basis=LatencyBasis.CORRECTED,
    )
