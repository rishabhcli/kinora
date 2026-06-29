"""Liveness self-tests: eventually / leads_to under weak fairness, lassos.

The fairness semantics are the subtle bit, so these models are built to pin it
down: a goal that is reachable but avoidable forever under *no* fairness yet
guaranteed under *weak* fairness must be reported as HOLDS only when the
progressing action is weakly fair, and FAIL (with a lasso) otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.verification.modelcheck import (
    Action,
    ModelChecker,
    Spec,
    eventually,
    leads_to,
)
from app.verification.modelcheck.spec import Fairness

# --------------------------------------------------------------------------- #
# A 2-state machine: idle <-> working, plus a one-way "finish" from working.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _S:
    where: str  # "idle" | "working" | "done"


def _machine(*, finish_fair: bool) -> Spec[_S]:
    start = Action[_S](
        name="start",
        guard=lambda s: s.where == "idle",
        effect=lambda s: (_S("working"),),
    )
    # Spin: working -> idle -> working forever (the tempting bad cycle).
    relax = Action[_S](
        name="relax",
        guard=lambda s: s.where == "working",
        effect=lambda s: (_S("idle"),),
    )
    finish = Action[_S](
        name="finish",
        guard=lambda s: s.where == "working",
        effect=lambda s: (_S("done"),),
        fairness=Fairness.WEAK if finish_fair else Fairness.NONE,
    )
    return Spec(
        name="machine",
        initial=(_S("idle"),),
        actions=(start, relax, finish),
        liveness=(eventually("reaches_done", lambda s: s.where == "done"),),
    )


def test_eventually_fails_without_fairness() -> None:
    # finish is optional: the run can spin idle<->working forever → lasso.
    report = ModelChecker[_S]().check(_machine(finish_fair=False))
    res = report.result_for("reaches_done")
    assert res is not None and not res.holds, report.render()
    assert res.counterexample is not None
    # The lasso loop must avoid "done".
    loop_states = {step.state.where for step in res.counterexample.loop}  # type: ignore[union-attr]
    assert "done" not in loop_states


def test_eventually_holds_under_weak_fairness() -> None:
    # finish is weakly fair: continuously enabled in "working", so any fair run
    # that lingers in working must eventually take it. The idle<->working cycle
    # keeps re-enabling finish, so fairness forces it → done is inevitable.
    report = ModelChecker[_S]().check(_machine(finish_fair=True))
    res = report.result_for("reaches_done")
    assert res is not None and res.holds, report.render()


# --------------------------------------------------------------------------- #
# leads_to: a request must be eventually served.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Req:
    requested: bool
    served: bool


def _request_machine(*, serve_fair: bool) -> Spec[_Req]:
    request = Action[_Req](
        name="request",
        guard=lambda s: not s.requested and not s.served,
        effect=lambda s: (_Req(requested=True, served=False),),
    )
    serve = Action[_Req](
        name="serve",
        guard=lambda s: s.requested and not s.served,
        effect=lambda s: (_Req(requested=True, served=True),),
        fairness=Fairness.WEAK if serve_fair else Fairness.NONE,
    )
    # idle churn keeps the bad cycle alive when serve is unfair.
    noop = Action[_Req](
        name="noop",
        guard=lambda s: s.requested and not s.served,
        effect=lambda s: (s,),
    )
    return Spec(
        name="request_machine",
        initial=(_Req(False, False),),
        actions=(request, serve, noop),
        leads_to_props=(
            leads_to(
                "request_served",
                trigger=lambda s: s.requested,
                goal=lambda s: s.served,
            ),
        ),
    )


def test_leads_to_fails_when_serve_unfair() -> None:
    report = ModelChecker[_Req]().check(_request_machine(serve_fair=False))
    res = report.result_for("request_served")
    assert res is not None and not res.holds, report.render()
    # the lasso must stay in the unserved region
    assert res.counterexample is not None
    assert all(not step.state.served for step in res.counterexample.loop)  # type: ignore[union-attr]


def test_leads_to_holds_when_serve_fair() -> None:
    report = ModelChecker[_Req]().check(_request_machine(serve_fair=True))
    res = report.result_for("request_served")
    assert res is not None and res.holds, report.render()


def test_eventually_trivially_true_when_goal_initial() -> None:
    # Goal holds in the only state → no ¬goal states → vacuously holds.
    stay = Action[_S](name="stay", guard=lambda s: True, effect=lambda s: (s,))
    spec = Spec(
        name="already_done",
        initial=(_S("done"),),
        actions=(stay,),
        liveness=(eventually("reaches_done", lambda s: s.where == "done"),),
    )
    report = ModelChecker[_S]().check(spec)
    assert report.ok, report.render()


def test_lasso_renders_readably() -> None:
    report = ModelChecker[_S]().check(_machine(finish_fair=False))
    res = report.result_for("reaches_done")
    assert res is not None and res.counterexample is not None
    text = res.counterexample.render(lambda s: s.where)
    assert "loop (repeats forever)" in text
    assert "working" in text
