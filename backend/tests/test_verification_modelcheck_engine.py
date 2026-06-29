"""Engine + DSL self-tests: reachability, safety traces, deadlock, symmetry.

These exercise the checker itself on tiny, hand-verifiable models (a bounded
counter, a mutex, a producer/consumer) so a failure here is the *checker's*
fault, not a protocol's. The protocol specs get their own suites.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.verification.modelcheck import (
    Action,
    ExplorationOrder,
    ModelChecker,
    Spec,
    invariant,
)
from app.verification.modelcheck.spec import Fairness
from app.verification.modelcheck.symmetry import SymmetryReduction, sort_multiset

# --------------------------------------------------------------------------- #
# A bounded counter: 0..N, +1 and -1, never leaves [0, N].
# --------------------------------------------------------------------------- #


def _counter_spec(n: int, *, broken: bool = False) -> Spec[int]:
    inc = Action[int](
        name="inc",
        guard=lambda s: s < n,
        effect=lambda s: (s + 1,),
        fairness=Fairness.WEAK,
    )
    # The broken variant lets the counter underflow past 0.
    dec = Action[int](
        name="dec",
        guard=lambda s: (s > -1 if broken else s > 0),
        effect=lambda s: (s - 1,),
        fairness=Fairness.WEAK,
    )
    return Spec(
        name=f"counter_{n}",
        initial=(0,),
        actions=(inc, dec),
        invariants=(invariant("non_negative", lambda s: s >= 0),),
    )


def test_counter_invariant_holds() -> None:
    report = ModelChecker[int]().check(_counter_spec(5))
    assert report.ok, report.render()
    assert report.states_explored == 6  # 0..5


def test_counter_invariant_violation_has_shortest_trace() -> None:
    report = ModelChecker[int]().check(_counter_spec(5, broken=True))
    res = report.result_for("non_negative")
    assert res is not None and not res.holds
    # Shortest path to a negative state: 0 --dec--> -1.
    cex = res.counterexample
    assert cex is not None
    assert cex.final_state == -1  # type: ignore[union-attr]
    assert cex.actions == ("dec",)  # type: ignore[union-attr]


def test_dfs_finds_same_violation() -> None:
    report = ModelChecker[int](order=ExplorationOrder.DFS).check(
        _counter_spec(5, broken=True)
    )
    assert not report.ok


def test_assert_ok_raises_on_violation() -> None:
    report = ModelChecker[int]().check(_counter_spec(3, broken=True))
    try:
        report.assert_ok()
    except AssertionError as exc:
        assert "non_negative" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected assert_ok to raise")


def test_halt_on_violation_catches_unbounded_bug_fast() -> None:
    # A counter that can grow forever (unbounded state space) but also dips
    # negative on the very first step. With stop_on_violation (default), the
    # check must halt at the breach and NOT run to the max_states cap.
    inc = Action[int](name="inc", guard=lambda s: True, effect=lambda s: (s + 1,))
    dec = Action[int](name="dec", guard=lambda s: True, effect=lambda s: (s - 1,))
    spec = Spec(
        name="unbounded_but_buggy",
        initial=(0,),
        actions=(inc, dec),
        invariants=(invariant("non_negative", lambda s: s >= 0),),
    )
    report = ModelChecker[int](max_states=1_000_000).check(spec)
    assert not report.ok
    # Halted early: a handful of states, not anywhere near the cap.
    assert report.states_explored < 100
    res = report.result_for("non_negative")
    assert res is not None and res.counterexample is not None
    assert res.counterexample.final_state == -1  # type: ignore[union-attr]


def test_stop_on_violation_disabled_explores_fully() -> None:
    # With the halt disabled and a bounded model, the full graph is explored and
    # the violation is still found (via the post-hoc BFS-forest scan).
    report = ModelChecker[int](stop_on_violation=False).check(
        _counter_spec(5, broken=True)
    )
    assert not report.ok
    assert report.result_for("non_negative").holds is False  # type: ignore[union-attr]


# --------------------------------------------------------------------------- #
# Deadlock detection.
# --------------------------------------------------------------------------- #


def test_deadlock_detected() -> None:
    # A machine that can step 0->1 then gets stuck at 1 (no action enabled).
    step = Action[int](name="step", guard=lambda s: s == 0, effect=lambda s: (1,))
    spec = Spec(name="stuck", initial=(0,), actions=(step,))
    report = ModelChecker[int](check_deadlock=True).check(spec)
    res = report.result_for("no_deadlock")
    assert res is not None and not res.holds
    assert res.counterexample is not None
    assert res.counterexample.final_state == 1  # type: ignore[union-attr]


def test_intended_terminal_is_not_a_deadlock() -> None:
    step = Action[int](name="step", guard=lambda s: s == 0, effect=lambda s: (1,))
    spec = Spec(name="done", initial=(0,), actions=(step,))
    report = ModelChecker[int](
        check_deadlock=True, is_terminal=lambda s: s == 1
    ).check(spec)
    assert report.ok, report.render()


# --------------------------------------------------------------------------- #
# Symmetry reduction: two interchangeable token slots.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _TwoSlots:
    slots: tuple[int, ...]  # each 0 or 1


def _two_slot_spec() -> Spec[_TwoSlots]:
    def flip(i: int) -> Action[_TwoSlots]:
        return Action[_TwoSlots](
            name=f"flip_{i}",
            guard=lambda s: True,
            effect=lambda s: (
                _TwoSlots(tuple(1 - v if j == i else v for j, v in enumerate(s.slots))),
            ),
        )

    return Spec(
        name="two_slots",
        initial=(_TwoSlots((0, 0)),),
        actions=(flip(0), flip(1)),
        invariants=(invariant("at_most_two_set", lambda s: sum(s.slots) <= 2),),
    )


def test_symmetry_collapses_orbit() -> None:
    spec = _two_slot_spec()
    full = ModelChecker[_TwoSlots]().check(spec)
    assert full.states_explored == 4  # (0,0)(0,1)(1,0)(1,1)

    reduction = SymmetryReduction.by(
        lambda s: _TwoSlots(sort_multiset(s.slots)),
        description="sort_slots",
    )
    reduced = ModelChecker[_TwoSlots](symmetry=reduction).check(spec)
    # (0,1) and (1,0) collapse to one representative → 3 states.
    assert reduced.states_explored == 3
    assert reduced.ok


def test_max_states_truncates() -> None:
    # An unbounded counter: inc forever. Bound it; expect truncation, not a hang.
    inc = Action[int](name="inc", guard=lambda s: True, effect=lambda s: (s + 1,))
    spec = Spec(name="unbounded", initial=(0,), actions=(inc,))
    report = ModelChecker[int](max_states=50).check(spec)
    assert report.truncated
    assert report.states_explored <= 50
