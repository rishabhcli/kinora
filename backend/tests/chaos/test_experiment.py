"""Tests for the declarative experiment/scenario model validation."""

from __future__ import annotations

import pytest

from app.chaos.experiment import AbortConditions, ChaosExperiment, ScheduledFault
from app.chaos.faults import ErrorFault, LatencyFault
from app.chaos.steady_state import SteadyStateHypothesis, availability_at_least


def _hyp() -> SteadyStateHypothesis:
    return SteadyStateHypothesis.of([availability_at_least(0.99)])


def test_valid_experiment_builds() -> None:
    exp = ChaosExperiment.of(
        name="ok",
        hypothesis=_hyp(),
        blast_radius=["dashscope"],
        schedule=[ScheduledFault(ErrorFault(dependency="dashscope", name="boom"), arm_at_s=1.0)],
        duration_s=10.0,
    )
    assert exp.name == "ok"
    assert exp.faults[0].name == "boom"


def test_fault_outside_blast_radius_rejected() -> None:
    with pytest.raises(ValueError, match="outside the blast radius"):
        ChaosExperiment.of(
            name="bad",
            hypothesis=_hyp(),
            blast_radius=["dashscope"],
            schedule=[ScheduledFault(ErrorFault(dependency="redis", name="x"))],
            duration_s=10.0,
        )


def test_empty_schedule_rejected() -> None:
    with pytest.raises(ValueError, match="at least one scheduled fault"):
        ChaosExperiment.of(
            name="bad", hypothesis=_hyp(), blast_radius=["d"], schedule=[]
        )


def test_empty_blast_radius_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty blast radius"):
        ChaosExperiment.of(
            name="bad",
            hypothesis=_hyp(),
            blast_radius=[],
            schedule=[ScheduledFault(ErrorFault(dependency="d", name="x"))],
        )


def test_fault_arming_after_duration_rejected() -> None:
    with pytest.raises(ValueError, match="at/after the experiment duration"):
        ChaosExperiment.of(
            name="bad",
            hypothesis=_hyp(),
            blast_radius=["d"],
            schedule=[ScheduledFault(ErrorFault(dependency="d", name="x"), arm_at_s=20.0)],
            duration_s=10.0,
        )


def test_scheduled_fault_disarm_offset() -> None:
    sf = ScheduledFault(LatencyFault(dependency="d", name="n"), arm_at_s=2.0, hold_s=5.0)
    assert sf.disarm_at_s == 7.0
    hold_forever = ScheduledFault(LatencyFault(dependency="d", name="n"), arm_at_s=2.0)
    assert hold_forever.disarm_at_s is None


def test_scheduled_fault_negative_offset_rejected() -> None:
    with pytest.raises(ValueError):
        ScheduledFault(ErrorFault(dependency="d", name="n"), arm_at_s=-1.0)


def test_abort_conditions_validation() -> None:
    with pytest.raises(ValueError):
        AbortConditions(breach_tolerance=0)
    with pytest.raises(ValueError):
        AbortConditions(max_injected_errors=-1)
    with pytest.raises(ValueError):
        AbortConditions(max_duration_s=0)
    # Valid.
    AbortConditions(max_injected_errors=10, max_duration_s=5.0, breach_tolerance=2)
