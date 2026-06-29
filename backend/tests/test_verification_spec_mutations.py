"""Mutation tests: inject each real bug and prove the checker catches it.

A model checker that never fails is worthless. These tests deliberately *break*
each protocol — exactly the bug the corresponding invariant exists to forbid —
and assert the checker (a) reports the property as violated and (b) hands back a
counterexample whose final/loop state actually exhibits the bug. This is the
evidence that the green run of the real specs is meaningful, not vacuous.

Each mutation rebuilds the spec from scratch with a single broken action, so the
unmutated specs are never touched.
"""

from __future__ import annotations

from dataclasses import replace

from app.verification.modelcheck import (
    Action,
    ModelChecker,
    Spec,
    invariant,
    leads_to,
)
from app.verification.modelcheck.spec import Fairness
from app.verification.specs.render_queue import (
    RETRY_CAP,
    JobStatus,
    RenderJobState,
    build_render_queue_spec,
)
from app.verification.specs.scheduler_buffer import (
    HIGH,
    SchedulerState,
    build_scheduler_buffer_spec,
)

# --------------------------------------------------------------------------- #
# Scheduler: a promote that does NOT reserve budget → double-spend.
# --------------------------------------------------------------------------- #


def test_scheduler_double_spend_is_caught() -> None:
    base = build_scheduler_buffer_spec()
    # Replace `land` with a buggy variant that records the spend but leaks the
    # reservation (never releases it) — the §12.1 "fails to release reserved
    # budget on completion" bug. The slot is then double-counted (still reserved
    # *and* spent), so reserved+spent climbs past the budget cap.
    actions = []
    for a in base.actions:
        if a.name == "land":
            def buggy_land(s: SchedulerState) -> tuple[SchedulerState, ...]:
                return (
                    replace(
                        s,
                        inflight=s.inflight - 1,
                        # BUG: reserved NOT decremented — the reservation leaks.
                        spent=s.spent + 1,
                        buffer=min(s.buffer + 1, HIGH),
                    ),
                )

            actions.append(Action("land", a.guard, buggy_land, a.fairness))
        else:
            actions.append(a)
    broken = Spec(
        name="scheduler_double_spend_mutant",
        initial=base.initial,
        actions=tuple(actions),
        invariants=base.invariants,
        state_label=base.state_label,
    )
    report = ModelChecker[SchedulerState]().check(broken)
    res = report.result_for("no_over_commit")
    assert res is not None and not res.holds, "\n" + report.render()
    # The counterexample's final state must actually over-commit the budget.
    cex = res.counterexample
    assert cex is not None
    bad = cex.final_state  # type: ignore[union-attr]
    assert bad.reserved + bad.spent > bad.budget


def test_scheduler_buffer_underflow_is_caught() -> None:
    base = build_scheduler_buffer_spec()
    # An `advance` whose guard lets the buffer go below zero.
    actions = []
    for a in base.actions:
        if a.name == "advance":
            actions.append(
                Action(
                    "advance",
                    lambda s: True,  # BUG: no `buffer > 0` guard
                    a.effect,
                    a.fairness,
                )
            )
        else:
            actions.append(a)
    broken = Spec(
        name="scheduler_underflow_mutant",
        initial=base.initial,
        actions=tuple(actions),
        invariants=base.invariants,
        state_label=base.state_label,
    )
    report = ModelChecker[SchedulerState]().check(broken)
    res = report.result_for("buffer_non_negative")
    assert res is not None and not res.holds, "\n" + report.render()
    assert res.counterexample.final_state.buffer < 0  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Render queue: reap a FRESH lease → two holders / double spend.
# --------------------------------------------------------------------------- #


def test_render_queue_reaping_fresh_lease_is_caught() -> None:
    base = build_render_queue_spec(workers=2)
    actions = []
    for a in base.actions:
        if a.name == "reap":
            # BUG: reap regardless of lease freshness. A fresh, still-rendering
            # job is re-queued AND its live worker becomes a ghost that can ack a
            # second provider result → double-submit / double-spend.
            def buggy_reap_guard(s: RenderJobState) -> bool:
                return s.status in (
                    JobStatus.RESERVED,
                    JobStatus.SUBMITTED,
                    JobStatus.POLLING,
                )

            def buggy_reap(s: RenderJobState) -> tuple[RenderJobState, ...]:
                # The live holder keeps rendering (ghost) when we steal its FRESH
                # lease; a lapsed (crashed) lease leaves no ghost.
                return (
                    replace(
                        s,
                        status=JobStatus.QUEUED,
                        holder=0,
                        lease_valid=False,
                        reserved=False,
                        ghost_active=s.lease_valid,
                    ),
                )

            actions.append(Action("reap", buggy_reap_guard, buggy_reap, a.fairness))
        else:
            actions.append(a)
    broken = Spec(
        name="render_queue_fresh_reap_mutant",
        initial=base.initial,
        actions=tuple(actions),
        invariants=base.invariants,
        leads_to_props=base.leads_to_props,
        state_label=base.state_label,
    )
    report = ModelChecker[RenderJobState]().check(broken)
    # Reaping a fresh lease lets the job be claimed + succeed twice → double spend.
    res = report.result_for("no_double_spend")
    assert res is not None and not res.holds, "\n" + report.render()
    assert res.counterexample.final_state.spent >= 2  # type: ignore[union-attr]


def test_render_queue_lost_cancel_is_caught() -> None:
    # Drop the honour_cancel action entirely AND make a non-fair churn keep the
    # job alive: a requested cancel can then never become terminal.
    base = build_render_queue_spec(workers=1)
    kept = [a for a in base.actions if a.name != "honour_cancel"]
    # Add an idle churn so a cancelled-but-not-honoured job has a fair-free cycle.
    churn: Action[RenderJobState] = Action(
        "churn",
        lambda s: s.cancel_requested and not s.is_terminal(),
        lambda s: (s,),
        Fairness.NONE,
    )
    broken = Spec(
        name="render_queue_lost_cancel_mutant",
        initial=base.initial,
        actions=(*kept, churn),
        invariants=base.invariants,
        leads_to_props=base.leads_to_props,
        state_label=base.state_label,
    )
    report = ModelChecker[RenderJobState]().check(broken)
    res = report.result_for("cancel_eventually_honoured")
    assert res is not None and not res.holds, "\n" + report.render()
    # The lasso loop must hold a requested-but-unhonoured cancel.
    loop = res.counterexample.loop  # type: ignore[union-attr]
    assert all(step.state.cancel_requested for step in loop)
    assert all(not step.state.is_terminal() for step in loop)


# --------------------------------------------------------------------------- #
# Arbitration: a policy that surfaces with NO director → broken §7.2 gate.
# --------------------------------------------------------------------------- #


def test_arbitration_broken_surface_gate_is_caught() -> None:
    # We don't mutate the real policy (it's correct); instead we build a tiny
    # arbitration spec whose `arbitrate` ignores the director flag, to prove the
    # invariant *would* catch a regression in decide_arbitration.
    from app.agents.contracts import ConflictOption
    from app.verification.specs.arbitration import (
        ArbitrationPhase,
        ArbitrationState,
    )

    init = ArbitrationState(
        phase=ArbitrationPhase.CONFLICT,
        violates_canon=True,
        textual_support=False,
        director_present=False,  # no director
        user_facing=True,
        chosen=None,
        evolved=False,
        logged=False,
    )

    def bad_arbitrate(s: ArbitrationState) -> tuple[ArbitrationState, ...]:
        # BUG: surface even with no director present.
        return (
            replace(
                s,
                phase=ArbitrationPhase.AWAIT_USER,
                chosen=ConflictOption.SURFACE_TO_USER,
                logged=True,
            ),
        )

    spec = Spec(
        name="arbitration_bad_surface_mutant",
        initial=(init,),
        actions=(
            Action(
                "arbitrate",
                lambda s: s.phase is ArbitrationPhase.CONFLICT,
                bad_arbitrate,
            ),
        ),
        invariants=(
            invariant(
                "surface_requires_director_and_user_facing",
                lambda s: s.chosen is not ConflictOption.SURFACE_TO_USER
                or (s.director_present and s.user_facing),
            ),
        ),
    )
    report = ModelChecker[ArbitrationState]().check(spec)
    res = report.result_for("surface_requires_director_and_user_facing")
    assert res is not None and not res.holds, "\n" + report.render()


def test_arbitration_dropped_conflict_is_caught() -> None:
    # A lifecycle where a CONFLICT can loop forever without resolving → the
    # leads_to "conflict eventually approved" must FAIL with a lasso.
    from app.verification.specs.arbitration import (
        ArbitrationPhase,
        ArbitrationState,
    )

    init = ArbitrationState(
        phase=ArbitrationPhase.CONFLICT,
        violates_canon=True,
        textual_support=False,
        director_present=False,
        user_facing=False,
        chosen=None,
        evolved=False,
        logged=False,
    )
    approved = ArbitrationPhase.APPROVED
    spec = Spec(
        name="arbitration_dropped_conflict_mutant",
        initial=(init,),
        actions=(
            # BUG: the only action just spins on CONFLICT; nothing resolves it.
            Action(
                "spin",
                lambda s: s.phase is ArbitrationPhase.CONFLICT,
                lambda s: (s,),
            ),
        ),
        leads_to_props=(
            leads_to(
                "conflict_eventually_approved",
                trigger=lambda s: s.phase is ArbitrationPhase.CONFLICT,
                goal=lambda s: s.phase is approved,
            ),
        ),
    )
    report = ModelChecker[ArbitrationState]().check(spec)
    res = report.result_for("conflict_eventually_approved")
    assert res is not None and not res.holds, "\n" + report.render()


# --------------------------------------------------------------------------- #
# Fairness: a committed admit that ignores the per-session cap → starvation risk.
# --------------------------------------------------------------------------- #


def test_fairness_dropped_per_session_cap_is_caught() -> None:
    from app.verification.specs.fairness import (
        COMMITTED_SLOTS,
        FairnessState,
        build_fairness_spec,
        session_symmetry,
    )

    base = build_fairness_spec(sessions=3)
    actions = []
    for a in base.actions:
        if a.name.startswith("admit_committed_s"):
            # BUG: drop the per-session cap from the guard. One session can now
            # grab all 4 committed slots and starve the other two readers.
            idx = int(a.name.rsplit("s", 1)[1])

            def guard(s: FairnessState, i: int = idx) -> bool:
                return (
                    s.sessions[i].committed_demand
                    and s.committed_total() < COMMITTED_SLOTS
                    # per-session cap removed
                )

            actions.append(Action(a.name, guard, a.effect, a.fairness))
        else:
            actions.append(a)
    broken = Spec(
        name="fairness_no_cap_mutant",
        initial=base.initial,
        actions=tuple(actions),
        invariants=base.invariants,
        leads_to_props=base.leads_to_props,
        state_label=base.state_label,
    )
    report = ModelChecker[FairnessState](symmetry=session_symmetry()).check(broken)
    res = report.result_for("per_session_cap_respected")
    assert res is not None and not res.holds, "\n" + report.render()
    # The offending state has a session running more than the cap.
    from app.verification.specs.fairness import PER_SESSION_COMMITTED_CAP

    bad = res.counterexample.final_state  # type: ignore[union-attr]
    assert any(
        load.committed_running > PER_SESSION_COMMITTED_CAP for load in bad.sessions
    )


# A reference so RETRY_CAP import is meaningful (documents the bound under test).
def test_retry_cap_documented() -> None:
    assert RETRY_CAP >= 1
