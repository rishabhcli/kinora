"""Recovery sweeps for the saga engine — make orphaned runs self-heal.

Two failure modes need a background sweep, both modelled here as pure,
clock-driven passes so they're testable without a scheduler or real time:

* **Due timers.** A run parked ``WAITING`` on a signal-await timeout must be
  re-driven once its deadline passes even if no signal ever arrives. The sweep
  finds runs whose timer ``fire_at`` <= now and resumes each (the engine then
  routes the await-timeout to its branch or compensation).

* **Stuck / abandoned runs.** A worker that crashed mid-step leaves a
  ``RUNNING`` / ``COMPENSATING`` run holding an *expired lease*. The sweep
  re-claims and resumes such runs so they continue from their cursor instead of
  hanging forever. (A still-live worker keeps its lease fresh, so the sweep
  won't steal it.)

A single :meth:`RecoverySweeper.sweep` call performs both passes and returns a
:class:`SweepReport`. Production runs it on an interval (the API's idle-sweeper);
tests call it directly after advancing a :class:`~app.sagas.clock.FakeClock`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.logging import get_logger
from app.sagas.clock import SYSTEM_CLOCK, Clock
from app.sagas.engine import SagaEngine
from app.sagas.errors import SagaError, SagaFailed
from app.sagas.history import RunState
from app.sagas.store import DurableStore
from app.sagas.telemetry import SagaEventType, TelemetryBus

logger = get_logger("app.sagas.recovery")


@dataclass(slots=True)
class SweepReport:
    """What one :meth:`RecoverySweeper.sweep` did."""

    fired_timers: list[str] = field(default_factory=list)
    recovered_stuck: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.fired_timers) + len(self.recovered_stuck)


class RecoverySweeper:
    """Re-drives due-timer and abandoned (expired-lease) runs."""

    def __init__(
        self,
        engine: SagaEngine,
        store: DurableStore,
        *,
        clock: Clock = SYSTEM_CLOCK,
        bus: TelemetryBus | None = None,
        batch: int = 100,
    ) -> None:
        self._engine = engine
        self._store = store
        self._clock = clock
        self._bus = bus or TelemetryBus()
        self._batch = batch

    async def sweep(self) -> SweepReport:
        """One full pass: fire due timers, then recover stuck runs."""
        report = SweepReport()
        now = self._clock.time()

        # 1) due timers (parked signal-awaits whose deadline passed)
        for parked in await self._store.due_runs(now, limit=self._batch):
            await self._resume(parked, report, kind="timer")

        # 2) stuck runs (RUNNING/COMPENSATING with an expired lease)
        for stuck in await self._store.stuck_runs(now, limit=self._batch):
            self._bus.emit(
                SagaEventType.RUN_RECOVERED,
                stuck.run_id,
                stuck.workflow,
                lease_until=stuck.lease_until,
                status=stuck.status,
            )
            await self._resume(stuck, report, kind="stuck")

        if report.total or report.failed:
            logger.info(
                "saga.recovery.sweep",
                fired=len(report.fired_timers),
                recovered=len(report.recovered_stuck),
                failed=len(report.failed),
            )
        return report

    async def _resume(self, run: RunState, report: SweepReport, *, kind: str) -> None:
        try:
            await self._engine.resume(run.run_id)
        except SagaFailed:
            # A resumed run that fails (e.g. await timed out → compensated) is a
            # *successful* recovery outcome, not a sweep error.
            (report.fired_timers if kind == "timer" else report.recovered_stuck).append(run.run_id)
        except SagaError:
            logger.warning("saga.recovery.error", run_id=run.run_id, kind=kind)
            report.failed.append(run.run_id)
        else:
            (report.fired_timers if kind == "timer" else report.recovered_stuck).append(run.run_id)


__all__ = ["RecoverySweeper", "SweepReport"]
