"""Run reports — a human-readable text table and a machine-readable JSON blob.

A run is only actionable if you can read its result and a CI job can parse it.
:func:`build_report` folds a finished :class:`~app.loadtest.generator.RunResult`,
an optional budget :class:`~app.loadtest.budget.GateResult`, and an optional
:class:`~app.loadtest.regression.RegressionReport` into one :class:`RunReport`
that renders to:

* :meth:`RunReport.to_text` — an aligned per-endpoint percentile table with the
  PASS/FAIL gate line and any regression findings, for a terminal / CI log.
* :meth:`RunReport.to_json` — the same data as a nested dict for storage, dashboards,
  or diffing the next run against this one as a baseline.

All latencies render in **milliseconds** for readability (collected in seconds).
Pure formatting; no I/O (the caller decides where bytes go).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.loadtest.budget import GateResult
from app.loadtest.collector import LatencyCollector
from app.loadtest.generator import RunResult
from app.loadtest.histogram import LatencySummary
from app.loadtest.regression import RegressionReport

_MS = 1000.0


def _summary_dict_ms(summary: LatencySummary) -> dict[str, float | int]:
    """A summary as a dict in milliseconds (count stays an int)."""
    return {
        "count": summary.count,
        "min_ms": summary.min * _MS,
        "max_ms": summary.max * _MS,
        "mean_ms": summary.mean * _MS,
        "p50_ms": summary.p50 * _MS,
        "p90_ms": summary.p90 * _MS,
        "p95_ms": summary.p95 * _MS,
        "p99_ms": summary.p99 * _MS,
        "p999_ms": summary.p999 * _MS,
    }


@dataclass(slots=True)
class RunReport:
    """A rendered view of a run + its gate + its regression verdict."""

    model: str
    scenario: str
    elapsed_s: float
    throughput_rps: float
    attempted: int
    dropped: int
    #: endpoint → corrected-latency summary dict (ms) + outcome counts. Typed
    #: ``Any`` because the values are heterogeneous nested JSON blobs that are
    #: only ever read positionally by the renderers below / serialized as-is.
    per_endpoint: Mapping[str, dict[str, Any]]
    aggregate: dict[str, Any]
    gate: dict[str, Any] | None = None
    regression: dict[str, Any] | None = None

    # ----- JSON ----------------------------------------------------------- #

    def to_dict(self) -> dict[str, object]:
        out: dict[str, object] = {
            "model": self.model,
            "scenario": self.scenario,
            "elapsed_s": self.elapsed_s,
            "throughput_rps": self.throughput_rps,
            "attempted": self.attempted,
            "dropped": self.dropped,
            "aggregate": self.aggregate,
            "per_endpoint": dict(self.per_endpoint),
        }
        if self.gate is not None:
            out["gate"] = self.gate
        if self.regression is not None:
            out["regression"] = self.regression
        return out

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    # ----- text ----------------------------------------------------------- #

    def to_text(self) -> str:
        lines: list[str] = []
        lines.append("=" * 78)
        lines.append(f"Kinora load run — model={self.model} scenario={self.scenario}")
        lines.append(
            f"elapsed={self.elapsed_s:.2f}s  throughput={self.throughput_rps:.1f} req/s  "
            f"attempted={self.attempted}  dropped={self.dropped}"
        )
        lines.append("-" * 78)
        header = (
            f"{'endpoint':<16}{'n':>7}{'p50':>9}{'p90':>9}{'p95':>9}"
            f"{'p99':>9}{'max':>9}{'err%':>7}"
        )
        lines.append(header)
        lines.append("-" * 78)
        for ep in sorted(self.per_endpoint):
            row = self.per_endpoint[ep]
            lines.append(self._format_row(ep, row["latency_ms"], row["counts"]))
        # Aggregate row.
        lines.append("-" * 78)
        lines.append(
            self._format_row(
                "ALL", self.aggregate["latency_ms"], self.aggregate["counts"]
            )
        )
        lines.append("=" * 78)
        if self.gate is not None:
            lines.append(f"GATE: {'PASS' if self.gate['passed'] else 'FAIL'}")
            for v in self.gate.get("violations", []):
                lines.append(
                    f"  - {v['endpoint']}: {v['metric']}={float(v['observed']):.4g} "
                    f"> {float(v['threshold']):.4g}"
                )
            for ep in self.gate.get("missing_endpoints", []):
                lines.append(f"  ? {ep}: budgeted but no samples recorded")
        if self.regression is not None:
            reg = self.regression
            lines.append(f"REGRESSION: {'YES' if reg['regressed'] else 'none'}")
            for f in reg.get("findings", []):
                lines.append(
                    f"  - {f['endpoint']}.{f['metric']}: "
                    f"{float(f['baseline']):.4g} -> {float(f['current']):.4g} "
                    f"({float(f['rel_delta']):+.1%})"
                )
        return "\n".join(lines)

    @staticmethod
    def _format_row(
        endpoint: str, lat: Mapping[str, float], counts: Mapping[str, Any]
    ) -> str:
        err_rate = float(counts.get("error_rate", 0.0)) * 100.0
        return (
            f"{endpoint:<16}{int(counts.get('total', 0)):>7}"
            f"{lat['p50_ms']:>9.1f}{lat['p90_ms']:>9.1f}{lat['p95_ms']:>9.1f}"
            f"{lat['p99_ms']:>9.1f}{lat['max_ms']:>9.1f}{err_rate:>7.2f}"
        )


def build_report(
    result: RunResult,
    *,
    gate: GateResult | None = None,
    regression: RegressionReport | None = None,
    corrected: bool = True,
) -> RunReport:
    """Fold a finished run (+ optional gate / regression) into a :class:`RunReport`."""
    collector: LatencyCollector = result.collector

    per_endpoint: dict[str, dict[str, object]] = {}
    for ep in collector.endpoints:
        st = collector.stats_for(ep)
        summary = (st.corrected if corrected else st.service).summary()
        per_endpoint[ep] = {
            "latency_ms": _summary_dict_ms(summary),
            "service_latency_ms": _summary_dict_ms(st.service.summary()),
            "counts": st.counts.as_dict(),
            "throughput_rps": collector.throughput_rps(endpoint=ep),
        }

    agg = collector.aggregate()
    agg_summary = (agg.corrected if corrected else agg.service).summary()
    aggregate = {
        "latency_ms": _summary_dict_ms(agg_summary),
        "service_latency_ms": _summary_dict_ms(agg.service.summary()),
        "counts": agg.counts.as_dict(),
        "throughput_rps": collector.throughput_rps(),
    }

    return RunReport(
        model=str(result.plan.model),
        scenario=result.plan.scenario.name,
        elapsed_s=collector.elapsed_s,
        throughput_rps=collector.throughput_rps(),
        attempted=result.attempted,
        dropped=result.dropped,
        per_endpoint=per_endpoint,
        aggregate=aggregate,
        gate=gate.as_dict() if gate is not None else None,
        regression=regression.as_dict() if regression is not None else None,
    )
