"""Unit tests for ConflictResolver._evolve_canon (§7.2/§8.5) — direct, no pipeline.

Regression coverage for an independent-review finding (2026-07-05):
``_evolve_canon`` used to re-assert the CITED state's own ``object_value`` when
``contradicting_state_id`` resolved against ``canon_slice.active_states``. But
that id only ever resolves there for a currently-ACTIVE fact being
contradicted (a retired fact structurally cannot appear in an active-states
slice, §8.4) — so the "precise" branch always re-wrote the OLD, just-
contradicted value forward, never the new one the shot actually depicts
(e.g. "evolving" forest→castle re-asserted "forest"). Fixed to always record
the honest, generic ``canon_evolved`` marker instead of a fabricated
structured re-assertion this seam can't actually derive.
"""

from __future__ import annotations

from typing import Any

from app.agents.contracts import (
    ConflictObject,
    ConflictOption,
    ConflictType,
    ContinuityResult,
    DecisionRecord,
)
from app.memory.interfaces import CanonSlice
from app.render.conflict import ConflictResolver
from tests.test_render_support import BEAT_ID, BOOK_ID, SCENE_ID, STATE_ID, FakeEvolver, make_slice

# ConflictResolver.resolve accepts `ShotSpec | str`; a plain string is enough
# since none of these fakes actually inspect the proposed shot.
_SHOT_SPEC = "the shot's proposed depiction"


class _FixedConflictContinuity:
    """Always raises the SAME structured conflict, whatever the caller wants to test."""

    def __init__(self, conflict: ConflictObject) -> None:
        self._conflict = conflict

    async def check_shot(
        self,
        proposed: Any,
        canon_slice: CanonSlice,
        *,
        shot_id: str | None = None,
        current_beat_id: str | None = None,
        target_duration_s: float = 5.0,
    ) -> ContinuityResult:
        return ContinuityResult(ok=False, conflict=self._conflict)


class _AlwaysEvolveArbiter:
    """Always arbitrates to EVOLVE_CANON — isolates _evolve_canon from the
    separately-tested §7.2 arbitration policy itself."""

    async def arbitrate(
        self,
        conflict: ConflictObject,
        source_span_text: str,
        *,
        director_present: bool,
        textual_support: Any = None,
    ) -> DecisionRecord:
        return DecisionRecord(
            conflict_id=conflict.conflict_id,
            chosen_option=ConflictOption.EVOLVE_CANON,
            reasoning="test forces evolve",
            evolved_canon=True,
        )


def _conflict(*, contradicting_state_id: str | None, claim: str) -> ConflictObject:
    return ConflictObject(
        conflict_id="cf_test",
        raised_by="test",
        type=ConflictType.CANON_VIOLATION,
        shot_id="shot_1",
        claim=claim,
        canon_fact="the established canon",
        current_beat=BEAT_ID,
        contradicting_state_id=contradicting_state_id,
        user_facing=True,
    )


async def _resolve(*, canon_slice: CanonSlice, conflict: ConflictObject) -> tuple[Any, FakeEvolver]:
    evolver = FakeEvolver()
    resolver = ConflictResolver(
        continuity=_FixedConflictContinuity(conflict),
        showrunner=_AlwaysEvolveArbiter(),
        canon=evolver,
    )
    result = await resolver.resolve(
        book_id=BOOK_ID,
        shot_spec=_SHOT_SPEC,
        canon_slice=canon_slice,
        source_span_text="the shot's source text",
        current_beat_id=BEAT_ID,
        current_beat_index=7,
        director_present=False,
    )
    return result, evolver


async def test_evolve_canon_never_reasserts_an_active_states_old_value() -> None:
    """Case A: contradicting_state_id matches a state that IS in
    canon_slice.active_states (char_x possesses "sword"). The write must NOT
    reuse that state's own subject/predicate/object_value (the OLD,
    just-contradicted fact) — it must record the generic evolved marker
    carrying the NEW claim instead."""
    # active_states=[StateSlice(state_id=STATE_ID, ..., object_value="sword")]
    canon_slice = make_slice()
    conflict = _conflict(
        contradicting_state_id=STATE_ID, claim="the hero now wields a castle key"
    )

    result, evolver = await _resolve(canon_slice=canon_slice, conflict=conflict)

    assert result.action == "regenerate"
    assert result.evolved is True
    assert len(evolver.asserts) == 1
    subject, predicate, object_value, valid_from_beat = evolver.asserts[0]
    # Never the cited state's own OLD fields.
    assert (subject, predicate, object_value) != ("char_x", "possesses", "sword")
    assert predicate == "canon_evolved"
    assert object_value == conflict.claim  # the NEW claim, not the old cited value
    assert valid_from_beat == 7


async def test_evolve_canon_records_generic_marker_when_state_not_found() -> None:
    """Case B: contradicting_state_id matches nothing in active_states (e.g. a
    retired fact, which structurally can never appear there). Must behave
    identically to Case A — the same honest generic marker, not a crash or a
    different shape."""
    canon_slice = make_slice()
    conflict = _conflict(
        contradicting_state_id="state_does_not_exist", claim="the hero reacquires the sword"
    )

    result, evolver = await _resolve(canon_slice=canon_slice, conflict=conflict)

    assert result.action == "regenerate"
    assert result.evolved is True
    assert len(evolver.asserts) == 1
    subject, predicate, object_value, valid_from_beat = evolver.asserts[0]
    assert subject == "char_x"  # canon_slice.characters[0].entity_key
    assert predicate == "canon_evolved"
    assert object_value == conflict.claim
    assert valid_from_beat == 7


async def test_evolve_canon_falls_back_to_story_subject_without_characters() -> None:
    """No characters on the slice at all → the subject falls back to the
    literal "story" placeholder rather than crashing on an empty list."""
    canon_slice = CanonSlice(book_id=BOOK_ID, beat_id=BEAT_ID, beat_index=7, scene_id=SCENE_ID)
    conflict = _conflict(contradicting_state_id=None, claim="the world itself has changed")

    _, evolver = await _resolve(canon_slice=canon_slice, conflict=conflict)

    subject, predicate, _, _ = evolver.asserts[0]
    assert subject == "story"
    assert predicate == "canon_evolved"
