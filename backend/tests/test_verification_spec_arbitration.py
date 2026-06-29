"""Check the §7.2 conflict-arbitration policy spec.

This drives the REAL :func:`app.agents.showrunner.decide_arbitration` over all
eight environment worlds (violation × textual-support × director × user-facing)
and proves the §7.2 hard gates over the whole reachable lifecycle: evolve only
with textual support, surface only with a present director on a user-facing
conflict, every approved conflict carries a logged decision, and every raised
conflict eventually reaches approved.
"""

from __future__ import annotations

from app.verification.modelcheck import ModelChecker
from app.verification.specs.arbitration import (
    ArbitrationState,
    build_arbitration_spec,
)


def test_arbitration_invariants_hold() -> None:
    spec = build_arbitration_spec()
    report = ModelChecker[ArbitrationState]().check(spec)
    assert report.ok, "\n" + report.render()


def test_evolve_requires_textual_support() -> None:
    spec = build_arbitration_spec()
    report = ModelChecker[ArbitrationState]().check(spec)
    res = report.result_for("evolve_requires_textual_support")
    assert res is not None and res.holds, "\n" + report.render()


def test_surface_requires_director_and_user_facing() -> None:
    spec = build_arbitration_spec()
    report = ModelChecker[ArbitrationState]().check(spec)
    res = report.result_for("surface_requires_director_and_user_facing")
    assert res is not None and res.holds, "\n" + report.render()


def test_every_conflict_eventually_approved() -> None:
    spec = build_arbitration_spec()
    report = ModelChecker[ArbitrationState]().check(spec)
    res = report.result_for("conflict_eventually_approved")
    assert res is not None and res.holds, "\n" + report.render()


def test_all_eight_worlds_explored() -> None:
    # 8 initial worlds, each running the full lifecycle; the graph must contain
    # states for every chosen option that the policy can produce.
    from app.agents.contracts import ConflictOption

    spec = build_arbitration_spec()
    checker = ModelChecker[ArbitrationState]()
    report = checker.check(spec)
    graph, _ = checker.explore(spec)
    chosen = {s.chosen for s in graph.edges if s.chosen is not None}
    # All three resolutions must be reachable across the worlds.
    assert ConflictOption.HONOR_CANON in chosen
    assert ConflictOption.EVOLVE_CANON in chosen
    assert ConflictOption.SURFACE_TO_USER in chosen
    assert report.ok
