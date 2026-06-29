"""Lightweight, dependency-free metrics for the saga engine.

Process-global counters/gauges incremented at each saga/step transition. We keep
this a tiny in-process tally (rather than reaching for prometheus_client here) so
the engine has no hard observability dependency and tests can assert on exact
counts; an exporter can read :func:`snapshot` and publish it. This mirrors the
shape of :mod:`app.jobs.metrics`.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

_LOCK = threading.Lock()


@dataclass
class _Counters:
    sagas_started: int = 0
    sagas_committed: int = 0
    sagas_compensated: int = 0
    sagas_failed: int = 0
    sagas_aborted: int = 0
    sagas_timed_out: int = 0
    steps_executed: int = 0
    steps_succeeded: int = 0
    steps_failed: int = 0
    steps_retried: int = 0
    steps_compensated: int = 0
    compensations_failed: int = 0
    effects_deduped: int = 0
    resumes: int = 0
    by_definition: dict[str, int] = field(default_factory=dict)


_COUNTERS = _Counters()


def reset() -> None:
    """Reset all counters (test isolation)."""
    global _COUNTERS
    with _LOCK:
        _COUNTERS = _Counters()


def _inc(field_name: str, amount: int = 1) -> None:
    with _LOCK:
        setattr(_COUNTERS, field_name, getattr(_COUNTERS, field_name) + amount)


def saga_started(definition: str) -> None:
    with _LOCK:
        _COUNTERS.sagas_started += 1
        _COUNTERS.by_definition[definition] = _COUNTERS.by_definition.get(definition, 0) + 1


def saga_committed() -> None:
    _inc("sagas_committed")


def saga_compensated() -> None:
    _inc("sagas_compensated")


def saga_failed() -> None:
    _inc("sagas_failed")


def saga_aborted() -> None:
    _inc("sagas_aborted")


def saga_timed_out() -> None:
    _inc("sagas_timed_out")


def step_executed() -> None:
    _inc("steps_executed")


def step_succeeded() -> None:
    _inc("steps_succeeded")


def step_failed() -> None:
    _inc("steps_failed")


def step_retried() -> None:
    _inc("steps_retried")


def step_compensated() -> None:
    _inc("steps_compensated")


def compensation_failed() -> None:
    _inc("compensations_failed")


def effect_deduped() -> None:
    _inc("effects_deduped")


def resumed() -> None:
    _inc("resumes")


def snapshot() -> dict[str, int | dict[str, int]]:
    """A copy of all current counter values (for an exporter or a test assertion)."""
    with _LOCK:
        return {
            "sagas_started": _COUNTERS.sagas_started,
            "sagas_committed": _COUNTERS.sagas_committed,
            "sagas_compensated": _COUNTERS.sagas_compensated,
            "sagas_failed": _COUNTERS.sagas_failed,
            "sagas_aborted": _COUNTERS.sagas_aborted,
            "sagas_timed_out": _COUNTERS.sagas_timed_out,
            "steps_executed": _COUNTERS.steps_executed,
            "steps_succeeded": _COUNTERS.steps_succeeded,
            "steps_failed": _COUNTERS.steps_failed,
            "steps_retried": _COUNTERS.steps_retried,
            "steps_compensated": _COUNTERS.steps_compensated,
            "compensations_failed": _COUNTERS.compensations_failed,
            "effects_deduped": _COUNTERS.effects_deduped,
            "resumes": _COUNTERS.resumes,
            "by_definition": dict(_COUNTERS.by_definition),
        }


__all__ = [
    "compensation_failed",
    "effect_deduped",
    "reset",
    "resumed",
    "saga_aborted",
    "saga_committed",
    "saga_compensated",
    "saga_failed",
    "saga_started",
    "saga_timed_out",
    "snapshot",
    "step_compensated",
    "step_executed",
    "step_failed",
    "step_retried",
    "step_succeeded",
]
