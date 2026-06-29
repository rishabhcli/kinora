"""Check the §4.5–§4.9 dual-watermark promotion spec.

These prove the buffer invariants over the *whole* reachable state space of the
abstract Scheduler, not a sampled schedule: the buffer never goes negative, no
double-spend of the budget, and the burst/idle hysteresis is respected. The
liveness checks prove the lane always settles (the burst-then-idle sawtooth).
"""

from __future__ import annotations

from app.verification.modelcheck import ModelChecker
from app.verification.specs.scheduler_buffer import (
    SchedulerState,
    build_scheduler_buffer_spec,
)


def test_scheduler_buffer_invariants_hold() -> None:
    spec = build_scheduler_buffer_spec()
    report = ModelChecker[SchedulerState]().check(spec)
    # Print the full report on failure so the trace is visible.
    assert report.ok, "\n" + report.render()


def test_scheduler_explores_a_real_state_space() -> None:
    # Guard against an accidentally-empty model: there must be a meaningful graph.
    spec = build_scheduler_buffer_spec()
    report = ModelChecker[SchedulerState]().check(spec)
    assert report.states_explored > 30
    assert not report.truncated


def test_no_double_spend_invariant_named() -> None:
    spec = build_scheduler_buffer_spec()
    report = ModelChecker[SchedulerState]().check(spec)
    res = report.result_for("no_over_commit")
    assert res is not None and res.holds, "\n" + report.render()


def test_buffer_never_negative() -> None:
    spec = build_scheduler_buffer_spec()
    report = ModelChecker[SchedulerState]().check(spec)
    res = report.result_for("buffer_non_negative")
    assert res is not None and res.holds, "\n" + report.render()


def test_larger_budget_still_holds() -> None:
    # A bigger budget grows the space but must not break any invariant.
    spec = build_scheduler_buffer_spec(initial_budget=6, max_trajectory=3)
    report = ModelChecker[SchedulerState]().check(spec)
    assert report.ok, "\n" + report.render()
