"""Regression detection — compare a run to a saved baseline (catch it before readers).

A latency budget is an *absolute* contract; a regression detector is a *relative*
one. Even a run that passes its budget can have silently gotten 40% slower at p99
since last week — exactly the kind of creeping regression that ships because no
single run failed a threshold. :func:`detect_regressions` compares a run's
per-endpoint percentiles (and error rate / throughput) against a
:class:`Baseline` captured from a known-good run, and flags any metric whose
delta exceeds a configurable tolerance.

Two tolerance modes per metric:

* **relative** — flag when ``(current − baseline) / baseline > rel_tol`` (e.g.
  p99 grew more than 15%). Robust across environments where absolute numbers
  drift but ratios shouldn't.
* **absolute** — flag when ``current − baseline > abs_tol`` seconds, used as a
  floor so tiny baselines don't make a 1 ms→2 ms jump (a 100% *relative* change)
  fire spuriously.

A metric must breach **both** the relative tolerance *and* an absolute floor to
be flagged (configurable), which is the standard way to make regression gates
quiet on noise but loud on real slowdowns. Throughput regresses *downward* and
error rate *upward*, so those are checked with the sign flipped.

Baselines round-trip to JSON (:meth:`Baseline.to_dict` / :meth:`from_dict`) so a
CI job can store last-good and diff against it. Pure and synchronous.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.loadtest.collector import LatencyCollector
from app.loadtest.histogram import LatencySummary

#: The percentile metrics compared for a latency regression (higher = worse).
_LATENCY_METRICS = ("p50", "p90", "p95", "p99", "p999")


@dataclass(frozen=True, slots=True)
class EndpointBaseline:
    """The recorded good-run metrics for one endpoint."""

    endpoint: str
    latency: Mapping[str, float]  # metric → seconds (p50..p999)
    error_rate: float
    throughput_rps: float

    def to_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "latency": dict(self.latency),
            "error_rate": self.error_rate,
            "throughput_rps": self.throughput_rps,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> EndpointBaseline:
        return cls(
            endpoint=str(d["endpoint"]),
            latency={k: float(v) for k, v in dict(d["latency"]).items()},
            error_rate=float(d["error_rate"]),
            throughput_rps=float(d["throughput_rps"]),
        )


@dataclass(frozen=True, slots=True)
class Baseline:
    """A full saved baseline: per-endpoint metrics + a label."""

    label: str
    endpoints: Mapping[str, EndpointBaseline]

    def to_dict(self) -> dict[str, object]:
        return {
            "label": self.label,
            "endpoints": {ep: b.to_dict() for ep, b in self.endpoints.items()},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Baseline:
        eps: Mapping[str, Any] = dict(d["endpoints"])
        return cls(
            label=str(d.get("label", "baseline")),
            endpoints={ep: EndpointBaseline.from_dict(v) for ep, v in eps.items()},
        )

    @classmethod
    def from_collector(
        cls, collector: LatencyCollector, *, label: str = "baseline", corrected: bool = True
    ) -> Baseline:
        """Capture a baseline from a finished, known-good run."""
        endpoints: dict[str, EndpointBaseline] = {}
        for ep in collector.endpoints:
            st = collector.stats_for(ep)
            summary = (st.corrected if corrected else st.service).summary()
            endpoints[ep] = EndpointBaseline(
                endpoint=ep,
                latency={m: float(getattr(summary, m)) for m in _LATENCY_METRICS},
                error_rate=st.counts.error_rate,
                throughput_rps=collector.throughput_rps(endpoint=ep),
            )
        return cls(label=label, endpoints=endpoints)


@dataclass(frozen=True, slots=True)
class Tolerance:
    """How much drift is allowed before a metric counts as a regression."""

    #: Fractional growth allowed for latency / error-rate (0.15 = +15%).
    rel_tol: float = 0.15
    #: Absolute floor (seconds) a latency change must also exceed to flag.
    abs_tol_s: float = 0.010
    #: Fractional *drop* in throughput allowed before flagging (0.15 = −15%).
    throughput_rel_tol: float = 0.15
    #: Absolute error-rate increase (points) that must also be exceeded.
    error_rate_abs_tol: float = 0.005
    #: Require both relative AND absolute breach (quiet on noise). When False,
    #: relative breach alone flags.
    require_both: bool = True


@dataclass(frozen=True, slots=True)
class RegressionFinding:
    """One metric that regressed beyond tolerance."""

    endpoint: str
    metric: str
    baseline: float
    current: float

    @property
    def delta(self) -> float:
        return self.current - self.baseline

    @property
    def rel_delta(self) -> float:
        return self.delta / self.baseline if self.baseline else float("inf")

    def message(self) -> str:
        return (
            f"{self.endpoint}.{self.metric}: {self.baseline:.4g} -> {self.current:.4g} "
            f"({self.rel_delta:+.1%})"
        )


@dataclass(frozen=True, slots=True)
class RegressionReport:
    """The verdict of a baseline comparison."""

    regressed: bool
    findings: Sequence[RegressionFinding]
    #: endpoints present now but absent from the baseline (informational).
    new_endpoints: Sequence[str] = field(default_factory=tuple)
    #: endpoints in the baseline but absent now (possible coverage loss).
    missing_endpoints: Sequence[str] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, object]:
        return {
            "regressed": self.regressed,
            "findings": [
                {
                    "endpoint": f.endpoint,
                    "metric": f.metric,
                    "baseline": f.baseline,
                    "current": f.current,
                    "rel_delta": f.rel_delta,
                }
                for f in self.findings
            ],
            "new_endpoints": list(self.new_endpoints),
            "missing_endpoints": list(self.missing_endpoints),
        }

    def summary_line(self) -> str:
        if not self.regressed:
            return "NO REGRESSION — all metrics within tolerance of baseline"
        return f"REGRESSION — {len(self.findings)} metric(s) beyond tolerance"


def _is_worse_higher(
    baseline: float, current: float, *, rel_tol: float, abs_tol: float, require_both: bool
) -> bool:
    """A 'higher is worse' metric (latency / error rate) regressed?"""
    abs_breach = (current - baseline) > abs_tol
    if baseline <= 0:
        # No meaningful ratio; fall back to the absolute floor alone.
        return abs_breach
    rel_breach = (current - baseline) / baseline > rel_tol
    return (rel_breach and abs_breach) if require_both else rel_breach


def detect_regressions(
    collector: LatencyCollector,
    baseline: Baseline,
    *,
    tolerance: Tolerance | None = None,
    corrected: bool = True,
) -> RegressionReport:
    """Compare a finished run to ``baseline`` and report metrics beyond tolerance."""
    tol = tolerance or Tolerance()
    findings: list[RegressionFinding] = []

    current_eps = set(collector.endpoints)
    baseline_eps = set(baseline.endpoints)
    new_eps = sorted(current_eps - baseline_eps)
    missing_eps = sorted(baseline_eps - current_eps)

    for ep in sorted(current_eps & baseline_eps):
        base = baseline.endpoints[ep]
        st = collector.stats_for(ep)
        summary: LatencySummary = (st.corrected if corrected else st.service).summary()

        for metric in _LATENCY_METRICS:
            base_v = base.latency.get(metric)
            if base_v is None:
                continue
            cur_v = float(getattr(summary, metric))
            if _is_worse_higher(
                base_v,
                cur_v,
                rel_tol=tol.rel_tol,
                abs_tol=tol.abs_tol_s,
                require_both=tol.require_both,
            ):
                findings.append(RegressionFinding(ep, metric, base_v, cur_v))

        # Error rate: higher is worse.
        cur_err = st.counts.error_rate
        if _is_worse_higher(
            base.error_rate,
            cur_err,
            rel_tol=tol.rel_tol,
            abs_tol=tol.error_rate_abs_tol,
            require_both=tol.require_both,
        ):
            findings.append(
                RegressionFinding(ep, "error_rate", base.error_rate, cur_err)
            )

        # Throughput: *lower* is worse — flip the comparison.
        cur_tput = collector.throughput_rps(endpoint=ep)
        if base.throughput_rps > 0:
            drop = (base.throughput_rps - cur_tput) / base.throughput_rps
            if drop > tol.throughput_rel_tol:
                findings.append(
                    RegressionFinding(ep, "throughput_rps", base.throughput_rps, cur_tput)
                )

    return RegressionReport(
        regressed=bool(findings),
        findings=tuple(findings),
        new_endpoints=tuple(new_eps),
        missing_endpoints=tuple(missing_eps),
    )
