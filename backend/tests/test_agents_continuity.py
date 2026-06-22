"""Unit tests for the Continuity Supervisor: a well-formed structured conflict on
a canon contradiction, and a clean pass otherwise. No network."""

from __future__ import annotations

from app.agents.continuity import Continuity, build_conflict
from app.agents.contracts import ConflictOption, ConflictType
from app.memory.interfaces import CanonSlice, StateSlice
from app.providers import Providers
from tests.test_agents_support import (
    JsonSequencer,
    providers,  # noqa: F401  (pytest fixture)
)


def _slice_with_unarmed_state() -> CanonSlice:
    # The hero is unarmed at this beat (the sword was lost upstream and retired);
    # a shot that draws a sword contradicts this active fact.
    state = StateSlice(
        state_id="state_hero_unarmed_001",
        subject_entity_key="char_hero",
        predicate="is",
        object_value="unarmed",
        valid_from_beat=34,
        valid_to_beat=None,
    )
    return CanonSlice(
        book_id="book_x", beat_id="beat_0039", beat_index=39, active_states=[state]
    )


async def test_check_shot_emits_structured_conflict(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer(  # type: ignore[method-assign]
        {
            "contradicts": True,
            "contradicting_state_id": "state_hero_unarmed_001",
            "claim": "shot depicts the heroine drawing a sword",
            "canon_fact": None,
            "reasoning": "the hero is unarmed at this beat",
        }
    )
    result = await Continuity(providers).check_shot(
        "the heroine draws a sword",
        _slice_with_unarmed_state(),
        shot_id="shot_00051",
        current_beat_id="beat_0039",
    )

    assert result.ok is False
    conflict = result.conflict
    assert conflict is not None
    assert conflict.raised_by == "continuity_supervisor"
    assert conflict.type is ConflictType.CANON_VIOLATION
    assert conflict.shot_id == "shot_00051"
    assert conflict.current_beat == "beat_0039"
    assert conflict.claim == "shot depicts the heroine drawing a sword"
    # canon_fact was backfilled deterministically from the cited active state.
    assert conflict.canon_fact is not None
    assert "state_hero_unarmed_001" in conflict.canon_fact
    # The three §7.2 options are present.
    assert {opt.id for opt in conflict.options} == {
        ConflictOption.HONOR_CANON,
        ConflictOption.SURFACE_TO_USER,
        ConflictOption.EVOLVE_CANON,
    }


async def test_check_shot_passes_when_no_contradiction(providers: Providers) -> None:  # noqa: F811
    providers.chat.chat_json = JsonSequencer({"contradicts": False})  # type: ignore[method-assign]
    result = await Continuity(providers).check_shot(
        "the heroine walks through the forest", _slice_with_unarmed_state()
    )
    assert result.ok is True
    assert result.conflict is None


def test_build_conflict_is_deterministic() -> None:
    from app.agents.continuity import ContinuityJudgment

    state = StateSlice(
        state_id="s1",
        subject_entity_key="char_hero",
        predicate="possesses",
        object_value="prop_nothing",
        valid_from_beat=34,
    )
    judgment = ContinuityJudgment(
        contradicts=True, contradicting_state_id="s1", claim="draws a sword"
    )
    conflict = build_conflict(
        judgment, shot_id="shot_9", current_beat="beat_40", active_states=[state]
    )
    assert conflict.conflict_id == "cf_shot_9"
    assert conflict.contradicting_state_id == "s1"
    assert conflict.canon_fact is not None and "s1" in conflict.canon_fact
