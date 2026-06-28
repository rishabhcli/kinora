"""Tests for evolve-canon propagation (§8.5): when canon evolves, prior facts on
the superseded functional channel are proposed for retirement, with proof traces.
Pure, network-free."""

from __future__ import annotations

from app.memory.interfaces import CanonSlice, StateSlice
from app.render.conflict import RetirementProposal, propagate_evolution


def _slice_with_open_location() -> CanonSlice:
    # The hero is canonically located in the forest from beat 10 (still open).
    state = StateSlice(
        state_id="state_loc_forest",
        subject_entity_key="char_hero",
        predicate="located_in",
        object_value="loc_forest",
        valid_from_beat=10,
        valid_to_beat=None,
    )
    return CanonSlice(
        book_id="book_x", beat_id="beat_0030", beat_index=30, active_states=[state]
    )


def test_propagate_evolution_proposes_retiring_superseded_fact() -> None:
    # Canon evolves: the hero is now in the castle from beat 30. The prior open
    # 'located_in forest' must be retired at 30 to keep the channel single-valued.
    proposals = propagate_evolution(
        _slice_with_open_location(),
        subject_entity_key="char_hero",
        predicate="located_in",
        object_value="loc_castle",
        at_beat=30,
    )
    assert len(proposals) == 1
    proposal = proposals[0]
    assert isinstance(proposal, RetirementProposal)
    assert proposal.state_id == "state_loc_forest"
    assert proposal.retire_at_beat == 30
    assert "loc_castle" in proposal.proof
    assert "loc_forest" in proposal.reason


def test_propagate_evolution_no_op_for_same_value() -> None:
    # Re-asserting the same location is not a supersede → nothing to retire.
    proposals = propagate_evolution(
        _slice_with_open_location(),
        subject_entity_key="char_hero",
        predicate="located_in",
        object_value="loc_forest",
        at_beat=30,
    )
    assert proposals == []


def test_propagate_evolution_no_op_for_unrelated_channel() -> None:
    # Evolving a different channel (wardrobe) leaves the location fact untouched.
    proposals = propagate_evolution(
        _slice_with_open_location(),
        subject_entity_key="char_hero",
        predicate="wearing",
        object_value="a red cloak",
        at_beat=30,
    )
    assert proposals == []
