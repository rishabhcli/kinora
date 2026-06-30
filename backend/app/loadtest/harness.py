"""One-call facade — run a plan, gate it, diff a baseline, render a report.

The individual layers (clock, target, generator, collector, budget, regression,
report) compose freely, but the common case is a single call: *run this scenario
under this load against this target, check it against a budget, diff it against
last-good, and give me the report.* :func:`run_load_test` is that call.

For tests, pass a :class:`~app.loadtest.clock.VirtualClock` and a
:class:`~app.loadtest.target.FakeTarget` and wrap the call in
``await clock.run(...)`` — the whole thing is deterministic and instant. For a
real run, pass a :class:`~app.loadtest.clock.WallClock` and a real HTTP target.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.loadtest.budget import GateResult, LatencyBudget, evaluate_budget
from app.loadtest.clock import Clock, WallClock
from app.loadtest.generator import LoadGenerator, LoadPlan, RunResult
from app.loadtest.regression import (
    Baseline,
    RegressionReport,
    Tolerance,
    detect_regressions,
)
from app.loadtest.report import RunReport, build_report
from app.loadtest.target import Target


@dataclass(slots=True)
class HarnessResult:
    """Everything a single harness invocation produces."""

    run: RunResult
    report: RunReport
    gate: GateResult | None = None
    regression: RegressionReport | None = None

    @property
    def passed(self) -> bool:
        """The overall verdict: budget met *and* no regression flagged."""
        gate_ok = self.gate is None or self.gate.passed
        reg_ok = self.regression is None or not self.regression.regressed
        return gate_ok and reg_ok


async def run_load_test(
    plan: LoadPlan,
    target: Target,
    *,
    clock: Clock | None = None,
    budget: LatencyBudget | None = None,
    baseline: Baseline | None = None,
    tolerance: Tolerance | None = None,
    max_inflight: int | None = None,
    corrected: bool = True,
) -> HarnessResult:
    """Run ``plan`` against ``target``, then gate + regression-check + report.

    Returns a :class:`HarnessResult`; nothing is printed or written (the caller
    decides). The generator only ever calls ``target.send`` — no infra, no spend.
    """
    generator = LoadGenerator(
        clock=clock or WallClock(), target=target, max_inflight=max_inflight
    )
    run = await generator.run(plan)

    gate = evaluate_budget(run.collector, budget) if budget is not None else None
    regression = (
        detect_regressions(
            run.collector, baseline, tolerance=tolerance, corrected=corrected
        )
        if baseline is not None
        else None
    )
    report = build_report(run, gate=gate, regression=regression, corrected=corrected)
    return HarnessResult(run=run, report=report, gate=gate, regression=regression)
