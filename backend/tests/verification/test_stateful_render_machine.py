"""Stateful / model-based tests for the §9.7 per-shot render state machine.

Two layers:

1. **Reference-model agreement.** A :class:`hypothesis.stateful.RuleBasedStateMachine`
   fires random transition commands at *both* the production
   :class:`ShotStateMachine` and the independent :class:`ReferenceShot`, and asserts
   they agree on every step — acceptance, rejection (``IllegalTransitionError``),
   the resulting state, the no-op self-edge, and the history length. If the shipped
   transition table ever drifts from the documented §9.7 diagram, the two disagree
   and the run shrinks to the offending edge.

2. **Structural invariants** of the transition table itself (the diagram is
   well-formed: terminals are sinks, a sink is reachable from everywhere, the
   ``ShotStatus`` projection is total, etc.).
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from app.render.states import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    IllegalTransitionError,
    RenderState,
    ShotStateMachine,
    is_allowed,
    to_status,
)
from app.verification.properties.state_model import (
    REFERENCE_EDGES,
    REFERENCE_TERMINAL,
    ReferenceShot,
    ref_is_allowed,
    terminal_is_reachable_from_every_state,
)

ALL_STATES = list(RenderState)
render_states = st.sampled_from(ALL_STATES)


# --------------------------------------------------------------------------- #
# 1. Reference-model agreement (the stateful machine)
# --------------------------------------------------------------------------- #


class ShotMachineModel(RuleBasedStateMachine):
    """Drive the real machine and the reference model in lockstep.

    Each rule attempts a transition to a randomly chosen state on both; the real
    machine and the model must accept/reject identically, end in the same state,
    and grow their history by the same amount.
    """

    def __init__(self) -> None:
        super().__init__()
        self.real = ShotStateMachine("shot_model")
        self.model = ReferenceShot()

    @rule(dst=render_states)
    def attempt_transition(self, dst: RenderState) -> None:
        model_will_accept = self.model.can_step(dst)
        real_history_before = len(self.real.history)
        model_history_before = len(self.model.history)

        if model_will_accept:
            real_result = self.real.step(dst)
            model_result = self.model.step(dst)
            assert real_result == model_result == self.model.state
            # A no-op self-edge records nothing; a real edge records exactly one.
            is_noop = dst == model_result and len(self.model.history) == model_history_before
            expected_growth = 0 if is_noop else 1
            assert len(self.real.history) - real_history_before == expected_growth
            assert len(self.model.history) - model_history_before == expected_growth
        else:
            # Both must reject the same illegal edge.
            raised_real = False
            try:
                self.real.step(dst)
            except IllegalTransitionError:
                raised_real = True
            assert raised_real, f"real machine accepted illegal {self.real.state} -> {dst}"
            raised_model = False
            try:
                self.model.step(dst)
            except ValueError:
                raised_model = True
            assert raised_model
            # A rejected edge leaves both states + histories untouched.
            assert len(self.real.history) == real_history_before

    @invariant()
    def states_agree(self) -> None:
        assert self.real.state == self.model.state

    @invariant()
    def terminal_agrees(self) -> None:
        assert self.real.is_terminal == self.model.is_terminal

    @invariant()
    def history_is_a_valid_walk(self) -> None:
        """Every recorded edge in the real machine's history is a legal §9.7 edge."""
        for event in self.real.history:
            assert is_allowed(event.src, event.dst)


TestShotMachineModel = ShotMachineModel.TestCase


# --------------------------------------------------------------------------- #
# 2. Transition-table structure (the diagram is well-formed)
# --------------------------------------------------------------------------- #


def test_production_table_matches_reference_diagram() -> None:
    """The shipped table equals the independently-transcribed §9.7 diagram.

    This is the single assertion that catches a silent edit to ALLOWED_TRANSITIONS:
    the production table and the hand-derived reference must be identical.
    """
    assert ALLOWED_TRANSITIONS == REFERENCE_EDGES
    assert TERMINAL_STATES == REFERENCE_TERMINAL


def test_terminal_states_are_sinks() -> None:
    for terminal in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[terminal] == frozenset()


def test_non_terminal_states_have_outgoing_edges() -> None:
    """No accidental dead-end: every non-terminal state can move somewhere."""
    for state, edges in ALLOWED_TRANSITIONS.items():
        if state not in TERMINAL_STATES:
            assert edges, f"{state} has no outgoing edge but is not terminal"


def test_a_sink_is_reachable_from_every_state() -> None:
    """Liveness: a shot can always reach Accepted/Degraded — it never wedges."""
    assert terminal_is_reachable_from_every_state()


@given(render_states)
def test_status_projection_is_total(state: RenderState) -> None:
    """``to_status`` maps every §9.7 state to a persisted ShotStatus (no KeyError)."""
    from app.db.models.enums import ShotStatus

    assert isinstance(to_status(state), ShotStatus)


@given(render_states, render_states)
def test_is_allowed_agrees_with_reference(src: RenderState, dst: RenderState) -> None:
    assert is_allowed(src, dst) == ref_is_allowed(src, dst)


@given(render_states)
def test_self_edge_is_a_noop_not_recorded(state: RenderState) -> None:
    """Stepping to the current state is an idempotent no-op (records no history)."""
    machine = ShotStateMachine("shot_noop", state=state)
    result = machine.step(state)
    assert result is state
    assert machine.history == []


@given(render_states, render_states)
def test_illegal_edges_raise_and_do_not_mutate(
    src: RenderState, dst: RenderState
) -> None:
    """An illegal edge raises and leaves the machine exactly where it was."""
    if src == dst or is_allowed(src, dst):
        return
    machine = ShotStateMachine("shot_illegal", state=src)
    try:
        machine.step(dst)
        raised = False
    except IllegalTransitionError as exc:
        raised = True
        assert exc.src is src and exc.dst is dst
    assert raised
    assert machine.state is src
    assert machine.history == []
