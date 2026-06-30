"""The game-day runner — orchestrates a chaos experiment end to end.

:class:`GameDayRunner` is the conductor. Given a :class:`ChaosExperiment`, a
:class:`~app.chaos.interceptor.FaultInjector` (the seam the system-under-test
routes its dependency calls through), and a **steady-state probe** (an async
callable the caller supplies that measures the system and returns a metric
snapshot), it:

1. **Gates** — refuses to run unless :func:`assert_chaos_armable` allows it
   (prod hard gate). This happens *before* anything is armed.
2. **Preflight** — samples the probe once with no faults; if the steady state is
   already breaching, it bails with :class:`Verdict.PREFLIGHT_FAILED` (you cannot
   learn anything by breaking an already-broken system).
3. **Scopes** — sets the injector's blast radius to the experiment's, so chaos
   physically cannot touch anything outside it.
4. **Runs the schedule** — steps the virtual clock in ``poll_interval_s`` ticks
   to ``duration_s``, arming/disarming faults as their offsets pass, polling the
   probe each tick and recording a :class:`SteadyStateSample`.
5. **Auto-aborts** — the instant the steady state breaches (for
   ``breach_tolerance`` consecutive polls), or an abort *condition* trips, it
   **halts and rolls back every fault** (``injector.disarm_all()``), then records
   the verdict and reason. Rollback also runs in a ``finally`` so an exception in
   the probe never leaves faults armed.
6. **Reports** — emits a :class:`FindingsReport` with the full evidence.

Determinism: the runner only sleeps through the injected clock, so with a
:class:`~app.chaos.clock.VirtualClock` a whole multi-minute game-day completes
instantly and identically every run.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping

import structlog

from app.chaos.clock import SYSTEM_CLOCK, Clock
from app.chaos.experiment import ChaosExperiment, ScheduledFault
from app.chaos.gate import GateSettings, assert_chaos_armable
from app.chaos.interceptor import FaultInjector
from app.chaos.report import FindingsReport, SteadyStateSample, Verdict

logger = structlog.get_logger("app.chaos.runner")

#: An async probe: measure the system-under-test, return a metric snapshot
#: (``{"availability": 0.99, "p99_latency_ms": 800.0, ...}``) keyed to the
#: experiment's steady-state bound metrics. The caller owns what it measures.
SteadyStateProbe = Callable[[], Awaitable[Mapping[str, float]]]


class GameDayRunner:
    """Executes one :class:`ChaosExperiment` against an injectable target."""

    def __init__(
        self,
        settings: GateSettings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._settings = settings
        self._clock: Clock = clock or SYSTEM_CLOCK

    async def run(
        self,
        experiment: ChaosExperiment,
        *,
        injector: FaultInjector,
        probe: SteadyStateProbe,
    ) -> FindingsReport:
        """Run ``experiment``, returning the findings. Never raises on a breach.

        The prod gate is the one thing that *does* raise (a refused environment is
        a programming/config error, not an experiment outcome).
        """
        # 1) Hard gate — refuse outside local/test even if a flag is set.
        assert_chaos_armable(self._settings)

        started = self._clock.monotonic()
        report = FindingsReport(
            experiment_name=experiment.name,
            verdict=Verdict.HELD,
            started_at=started,
            ended_at=started,
            seed=experiment.seed,
        )

        # 3) Scope the blast radius up front (before any fault is armed).
        injector.set_scope(set(experiment.blast_radius))
        injector.disarm_all()
        injector.reset_timeline()

        try:
            # 2) Preflight: confirm the system is healthy with no chaos active.
            pre_snapshot = await probe()
            pre_result = experiment.hypothesis.evaluate(pre_snapshot)
            report.samples.append(
                SteadyStateSample(
                    monotonic_at=self._clock.monotonic(),
                    result=pre_result,
                    armed_dependencies=(),
                )
            )
            if not pre_result.held:
                report.verdict = Verdict.PREFLIGHT_FAILED
                report.abort_reason = (
                    "steady state already breaching before any fault: "
                    + ", ".join(b.bound.metric for b in pre_result.breached)
                )
                report.breaching_metrics = [b.bound.metric for b in pre_result.breached]
                logger.warning(
                    "chaos_preflight_failed",
                    experiment=experiment.name,
                    breached=report.breaching_metrics,
                )
                return self._finalize(report)

            # 4 + 5) Step the schedule, poll, auto-abort on breach.
            await self._run_schedule(experiment, injector=injector, probe=probe, report=report)
        finally:
            # Capture the full call timeline as evidence *before* rolling back.
            report.call_timeline = injector.timeline
            # 5) Rollback is unconditional — an exception or normal end both
            # leave the system fault-free.
            injector.disarm_all()

        return self._finalize(report)

    async def _run_schedule(
        self,
        experiment: ChaosExperiment,
        *,
        injector: FaultInjector,
        probe: SteadyStateProbe,
        report: FindingsReport,
    ) -> None:
        pending: list[ScheduledFault] = sorted(experiment.schedule, key=lambda s: s.arm_at_s)
        armed: list[ScheduledFault] = []
        consecutive_breaches = 0
        elapsed = 0.0
        interval = experiment.poll_interval_s

        while elapsed <= experiment.duration_s + 1e-9:
            # Arm any faults whose offset has arrived.
            for entry in list(pending):
                if entry.arm_at_s <= elapsed + 1e-9:
                    injector.arm(entry.fault)
                    armed.append(entry)
                    pending.remove(entry)
                    logger.info(
                        "chaos_fault_armed",
                        experiment=experiment.name,
                        fault=entry.fault.name,
                        dependency=entry.fault.dependency,
                        at_s=elapsed,
                    )
            # Disarm any faults whose hold window has elapsed.
            for entry in list(armed):
                disarm_at = entry.disarm_at_s
                if disarm_at is not None and disarm_at <= elapsed + 1e-9:
                    injector.disarm(entry.fault.dependency)
                    armed.remove(entry)

            # Poll the steady state.
            snapshot = await probe()
            result = experiment.hypothesis.evaluate(snapshot)
            report.samples.append(
                SteadyStateSample(
                    monotonic_at=self._clock.monotonic(),
                    result=result,
                    armed_dependencies=tuple(sorted(injector.armed_dependencies)),
                )
            )

            # Auto-abort: steady-state breach (with tolerance for blips).
            if not result.held:
                consecutive_breaches += 1
                if consecutive_breaches >= experiment.abort.breach_tolerance:
                    report.verdict = Verdict.BREACHED
                    report.breaching_metrics = [b.bound.metric for b in result.breached]
                    report.abort_reason = (
                        "auto-abort: steady state breached on "
                        + ", ".join(report.breaching_metrics)
                        + f" (tolerance={experiment.abort.breach_tolerance})"
                    )
                    logger.warning(
                        "chaos_auto_abort",
                        experiment=experiment.name,
                        breached=report.breaching_metrics,
                        at_s=elapsed,
                    )
                    return
            else:
                consecutive_breaches = 0

            # Abort conditions (additional guardrails).
            abort_reason = self._check_abort_conditions(experiment, injector, elapsed)
            if abort_reason is not None:
                report.verdict = Verdict.ABORTED
                report.abort_reason = abort_reason
                logger.warning(
                    "chaos_abort_condition",
                    experiment=experiment.name,
                    reason=abort_reason,
                    at_s=elapsed,
                )
                return

            # Advance the (virtual) clock by one poll interval.
            await self._clock.sleep(interval)
            elapsed += interval

    @staticmethod
    def _check_abort_conditions(
        experiment: ChaosExperiment, injector: FaultInjector, elapsed: float
    ) -> str | None:
        abort = experiment.abort
        if abort.max_injected_errors is not None:
            raised = sum(1 for c in injector.timeline if c.raised is not None)
            if raised > abort.max_injected_errors:
                return (
                    f"abort condition: {raised} injected errors exceeded cap "
                    f"{abort.max_injected_errors}"
                )
        if abort.max_duration_s is not None and elapsed >= abort.max_duration_s:
            return f"abort condition: elapsed {elapsed:.1f}s reached cap {abort.max_duration_s}s"
        return None

    def _finalize(self, report: FindingsReport) -> FindingsReport:
        report.ended_at = self._clock.monotonic()
        logger.info("chaos_gameday_done", summary=report.summary_line())
        return report


__all__ = ["GameDayRunner", "SteadyStateProbe"]
