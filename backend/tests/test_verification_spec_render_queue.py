"""Check the §12.1 render-queue claim/lease/ack lifecycle spec.

These prove, over every interleaving of two workers + a reaper + retries +
cancellation, that a job's budget is debited at most once (no double-spend), a
job is held by at most one worker, a terminal job releases all its resources,
and a fresh (heartbeated) lease is never reaped. The leads-to checks prove every
claimed job terminates and every cancel is eventually honoured.
"""

from __future__ import annotations

from app.verification.modelcheck import ModelChecker
from app.verification.modelcheck.symmetry import SymmetryReduction
from app.verification.specs.render_queue import (
    RenderJobState,
    build_render_queue_spec,
)


def test_render_queue_invariants_hold() -> None:
    spec = build_render_queue_spec(workers=2)
    report = ModelChecker[RenderJobState]().check(spec)
    assert report.ok, "\n" + report.render()


def test_no_double_spend() -> None:
    spec = build_render_queue_spec(workers=2)
    report = ModelChecker[RenderJobState]().check(spec)
    res = report.result_for("no_double_spend")
    assert res is not None and res.holds, "\n" + report.render()


def test_claimed_job_terminates() -> None:
    spec = build_render_queue_spec(workers=2)
    report = ModelChecker[RenderJobState]().check(spec)
    res = report.result_for("claimed_job_terminates")
    assert res is not None and res.holds, "\n" + report.render()


def test_cancel_eventually_honoured() -> None:
    spec = build_render_queue_spec(workers=2)
    report = ModelChecker[RenderJobState]().check(spec)
    res = report.result_for("cancel_eventually_honoured")
    assert res is not None and res.holds, "\n" + report.render()


def test_fresh_lease_never_reaped_via_holder_consistency() -> None:
    # holder_iff_leased + reserved_only_when_leased together encode "a fresh lease
    # protects the job"; if the reaper could steal a fresh lease we'd see a
    # second holder / a double spend, both invariants here.
    spec = build_render_queue_spec(workers=2)
    report = ModelChecker[RenderJobState]().check(spec)
    assert report.result_for("holder_iff_leased").holds  # type: ignore[union-attr]
    assert report.result_for("single_holder").holds  # type: ignore[union-attr]


def test_three_workers_still_safe() -> None:
    spec = build_render_queue_spec(workers=3)
    report = ModelChecker[RenderJobState]().check(spec)
    assert report.ok, "\n" + report.render()


def test_symmetry_reduction_preserves_result() -> None:
    # Workers are interchangeable: canonicalise the holder id away by mapping any
    # non-zero holder to 1 (the property only asks "is *some* worker holding it",
    # never *which*). Reduced exploration must reach the same verdict and fewer
    # states.
    spec = build_render_queue_spec(workers=2)
    full = ModelChecker[RenderJobState]().check(spec)

    def canon(s: RenderJobState) -> RenderJobState:
        from dataclasses import replace

        return replace(s, holder=1 if s.holder != 0 else 0)

    reduction = SymmetryReduction.by(canon, description="worker_orbit")
    reduced = ModelChecker[RenderJobState](symmetry=reduction).check(spec)
    assert reduced.ok
    assert reduced.states_explored <= full.states_explored
