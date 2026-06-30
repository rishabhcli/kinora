"""A tiny in-process event bus for saga lifecycle observability.

The engine emits a structured :class:`SagaEvent` at every meaningful transition
(step started/completed/retried/failed, compensation run, timer armed/fired,
signal delivered, run terminal). Production wires this to metrics/SSE; tests
attach a :class:`RecordingBus` to assert *exactly* which transitions happened —
e.g. that a resumed step emitted ``step_skipped`` and never re-ran, or that
compensations fired in reverse order.

The bus is intentionally synchronous and exception-swallowing: observability must
never change the engine's control flow or crash a run.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.sagas.telemetry")


class SagaEventType(StrEnum):
    """The vocabulary of saga lifecycle events."""

    RUN_STARTED = "run_started"
    RUN_RESUMED = "run_resumed"
    STEP_STARTED = "step_started"
    STEP_SKIPPED = "step_skipped"  # replayed from history; action not re-run
    STEP_COMPLETED = "step_completed"
    STEP_RETRYING = "step_retrying"
    STEP_FAILED = "step_failed"
    STEP_TIMEOUT = "step_timeout"
    STEP_BRANCHED = "step_branched"
    TIMER_ARMED = "timer_armed"
    TIMER_FIRED = "timer_fired"
    SIGNAL_WAIT = "signal_wait"
    SIGNAL_DELIVERED = "signal_delivered"
    COMPENSATION_STARTED = "compensation_started"
    COMPENSATION_OK = "compensation_ok"
    COMPENSATION_FAILED = "compensation_failed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    RUN_RECOVERED = "run_recovered"


@dataclass(frozen=True, slots=True)
class SagaEvent:
    """A single observability event with arbitrary structured fields."""

    type: SagaEventType
    run_id: str
    workflow: str
    step: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)


Observer = Callable[[SagaEvent], None]


class TelemetryBus:
    """A synchronous fan-out bus; observers must not raise (they're shielded)."""

    __slots__ = ("_observers",)

    def __init__(self, observers: list[Observer] | None = None) -> None:
        self._observers: list[Observer] = list(observers or [])

    def subscribe(self, observer: Observer) -> None:
        self._observers.append(observer)

    def emit(
        self,
        type_: SagaEventType,
        run_id: str,
        workflow: str,
        *,
        step: str | None = None,
        **fields: Any,
    ) -> None:
        event = SagaEvent(type=type_, run_id=run_id, workflow=workflow, step=step, fields=fields)
        for obs in self._observers:
            try:
                obs(event)
            except Exception:  # noqa: BLE001 - observability must never break a run
                logger.warning("saga.telemetry.observer_error", type=type_, run_id=run_id)


class RecordingBus(TelemetryBus):
    """A bus that retains every event (test assertions)."""

    __slots__ = ("events",)

    def __init__(self) -> None:
        super().__init__()
        self.events: list[SagaEvent] = []
        self.subscribe(self.events.append)

    def types(self) -> list[SagaEventType]:
        return [e.type for e in self.events]

    def of(self, type_: SagaEventType) -> list[SagaEvent]:
        return [e for e in self.events if e.type == type_]

    def steps_of(self, type_: SagaEventType) -> list[str | None]:
        return [e.step for e in self.events if e.type == type_]


__all__ = [
    "Observer",
    "RecordingBus",
    "SagaEvent",
    "SagaEventType",
    "TelemetryBus",
]
