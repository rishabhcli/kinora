"""Check the §12.2 lane-admission + per-session-fairness spec.

These prove, over every interleaving of N readers contending for the shared 4
committed / 2 speculative render slots: the lanes never exceed their sizes
(preemption + backpressure keep them bounded), no session ever exceeds its
per-session committed cap (the anti-starvation guard), and a saturated committed
lane always drains (the liveness side of fairness). The session-symmetry
reduction is checked to preserve the verdict while shrinking the space.
"""

from __future__ import annotations

from app.verification.modelcheck import ModelChecker
from app.verification.specs.fairness import (
    FairnessState,
    build_fairness_spec,
    session_symmetry,
)


def test_fairness_invariants_hold() -> None:
    spec = build_fairness_spec(sessions=3)
    report = ModelChecker[FairnessState](symmetry=session_symmetry()).check(spec)
    assert report.ok, "\n" + report.render()


def test_per_session_cap_respected() -> None:
    spec = build_fairness_spec(sessions=3)
    report = ModelChecker[FairnessState](symmetry=session_symmetry()).check(spec)
    res = report.result_for("per_session_cap_respected")
    assert res is not None and res.holds, "\n" + report.render()


def test_committed_lane_never_overflows() -> None:
    spec = build_fairness_spec(sessions=3)
    report = ModelChecker[FairnessState](symmetry=session_symmetry()).check(spec)
    assert report.result_for("committed_lane_bounded").holds  # type: ignore[union-attr]
    assert report.result_for("speculative_lane_bounded").holds  # type: ignore[union-attr]


def test_saturated_lane_drains() -> None:
    spec = build_fairness_spec(sessions=3)
    report = ModelChecker[FairnessState](symmetry=session_symmetry()).check(spec)
    res = report.result_for("saturated_committed_lane_drains")
    assert res is not None and res.holds, "\n" + report.render()


def test_symmetry_reduction_shrinks_and_preserves() -> None:
    spec = build_fairness_spec(sessions=3)
    full = ModelChecker[FairnessState]().check(spec)
    reduced = ModelChecker[FairnessState](symmetry=session_symmetry()).check(spec)
    assert full.ok and reduced.ok
    # The session-orbit canonicalisation must collapse permutation-equivalent
    # states, so the reduced space is strictly smaller.
    assert reduced.states_explored < full.states_explored


def test_more_sessions_still_fair() -> None:
    # Four readers against four committed slots: contention is real, but the
    # per-session cap and lane bounds must still hold.
    spec = build_fairness_spec(sessions=4)
    report = ModelChecker[FairnessState](symmetry=session_symmetry()).check(spec)
    assert report.ok, "\n" + report.render()
