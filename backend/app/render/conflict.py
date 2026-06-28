"""Conflict arbitration wiring — Critic → Continuity → Showrunner → apply (§7.2).

When the Critic flags a *timeline* failure (``repair_action`` ``raise_conflict``
or ``evolve_canon``, §9.5), the repair is not a render bug — it is a canon
question. This module wires the §7.2 negotiation protocol end-to-end:

1. **Continuity** validates the proposed depiction against the active canon and,
   on a violation, builds the structured :class:`ConflictObject` (it owns
   conflict construction). If Continuity clears it, the alarm was a false
   positive and the shot is approved (the §7.2 ``Checked → Approved`` edge).
2. **Showrunner** arbitrates the conflict under the fixed policy
   (``decide_arbitration``): evolve only with textual support, else surface to a
   present director, else honor the canon.
3. The decision is **applied**: ``honor_canon`` → regenerate empty-handed (a
   directive to drop the offending element); ``evolve_canon`` → write the change
   into canon via memory, then regenerate; ``surface_to_user`` → return the
   conflict for the UI to prompt the director.

The collaborators are typed as Protocols so the real ``Continuity`` /
``Showrunner`` / ``CanonService`` fit, and test doubles fit without inheritance.
The policy itself stays in the agents layer (``decide_arbitration``); this module
only sequences the calls and applies the outcome.
"""

from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from app.agents.contracts import (
    ConflictObject,
    ConflictOption,
    ContinuityResult,
    DecisionRecord,
    ShotSpec,
    TextualSupport,
)
from app.core.logging import get_logger
from app.memory.interfaces import CanonSlice, StateSlice

logger = get_logger("app.render.conflict")

ResolutionAction = Literal["accept", "regenerate", "surface"]


# --------------------------------------------------------------------------- #
# Collaborator protocols (real agents + canon service satisfy these)
# --------------------------------------------------------------------------- #


class ContinuityChecker(Protocol):
    """The Continuity Supervisor seam: validate a shot, build a conflict."""

    async def check_shot(
        self,
        proposed: ShotSpec | str,
        canon_slice: CanonSlice,
        *,
        shot_id: str | None = None,
        current_beat_id: str | None = None,
        target_duration_s: float = 5.0,
    ) -> ContinuityResult: ...


class Arbiter(Protocol):
    """The Showrunner seam: arbitrate a conflict into a decision record."""

    async def arbitrate(
        self,
        conflict: ConflictObject,
        source_span_text: str,
        *,
        director_present: bool,
        textual_support: TextualSupport | None = None,
    ) -> DecisionRecord: ...


class CanonEvolver(Protocol):
    """The canon-write seam used by ``evolve_canon`` (CanonService satisfies it)."""

    async def assert_state(
        self,
        *,
        book_id: str,
        subject_entity_key: str,
        predicate: str,
        object_value: str,
        valid_from_beat: int,
        source_span: dict[str, object] | None = None,
        state_id: str | None = None,
    ) -> str: ...


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


class ConflictResolution(BaseModel):
    """The applied outcome of a conflict (what the pipeline should do next)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    action: ResolutionAction
    honored: bool = False
    evolved: bool = False
    surfaced: bool = False
    decision: DecisionRecord | None = None
    conflict: ConflictObject | None = None
    evolved_state_id: str | None = None
    #: A directive folded into the next shot design when ``action == "regenerate"``.
    regen_directive: str = ""
    note: str = ""


class ConflictResolver:
    """Sequences Continuity → Showrunner and applies the §7.2 decision."""

    def __init__(
        self,
        *,
        continuity: ContinuityChecker,
        showrunner: Arbiter,
        canon: CanonEvolver,
    ) -> None:
        self._continuity = continuity
        self._showrunner = showrunner
        self._canon = canon

    async def resolve(
        self,
        *,
        book_id: str,
        shot_spec: ShotSpec | str,
        canon_slice: CanonSlice,
        source_span_text: str,
        current_beat_id: str,
        current_beat_index: int,
        director_present: bool,
        shot_id: str | None = None,
        target_duration_s: float = 5.0,
        textual_support: TextualSupport | None = None,
    ) -> ConflictResolution:
        """Run the §7.2 flow and return the applied resolution.

        ``textual_support`` may be injected so the evolve/surface/honor branches
        are exercisable without a model call; when omitted the Showrunner judges
        it from ``source_span_text``.
        """
        result = await self._continuity.check_shot(
            shot_spec,
            canon_slice,
            shot_id=shot_id,
            current_beat_id=current_beat_id,
            target_duration_s=target_duration_s,
        )
        if result.ok or result.conflict is None:
            logger.info("conflict.cleared", shot_id=shot_id, beat=current_beat_id)
            return ConflictResolution(action="accept", note="continuity cleared the alarm")

        conflict = result.conflict
        decision = await self._showrunner.arbitrate(
            conflict,
            source_span_text,
            director_present=director_present,
            textual_support=textual_support,
        )
        logger.info(
            "conflict.arbitrated",
            conflict_id=conflict.conflict_id,
            chosen=decision.chosen_option.value,
            shot_id=shot_id,
        )
        return await self._apply(
            conflict=conflict,
            decision=decision,
            book_id=book_id,
            canon_slice=canon_slice,
            current_beat_index=current_beat_index,
            source_span_text=source_span_text,
        )

    async def _apply(
        self,
        *,
        conflict: ConflictObject,
        decision: DecisionRecord,
        book_id: str,
        canon_slice: CanonSlice,
        current_beat_index: int,
        source_span_text: str,
    ) -> ConflictResolution:
        if decision.chosen_option is ConflictOption.SURFACE_TO_USER:
            return ConflictResolution(
                action="surface",
                surfaced=True,
                decision=decision,
                conflict=conflict,
                note="surfaced to the director for a choice",
            )

        if decision.chosen_option is ConflictOption.EVOLVE_CANON:
            state_id = await self._evolve_canon(
                conflict=conflict,
                book_id=book_id,
                canon_slice=canon_slice,
                at_beat=current_beat_index,
                source_span_text=source_span_text,
            )
            return ConflictResolution(
                action="regenerate",
                evolved=True,
                decision=decision,
                conflict=conflict,
                evolved_state_id=state_id,
                regen_directive=(
                    f"Canon evolved (textual support): {conflict.claim}. "
                    "Render the new state as established."
                ),
                note="canon evolved; regenerating with the new state",
            )

        # Default / honor_canon: regenerate empty-handed, honouring the canon.
        canon_fact = conflict.canon_fact or "the established canon"
        return ConflictResolution(
            action="regenerate",
            honored=True,
            decision=decision,
            conflict=conflict,
            regen_directive=(
                f"Honor canon: do NOT depict the contradiction ({conflict.claim}); "
                f"respect {canon_fact}."
            ),
            note="honouring canon; regenerating empty-handed",
        )

    async def _evolve_canon(
        self,
        *,
        conflict: ConflictObject,
        book_id: str,
        canon_slice: CanonSlice,
        at_beat: int,
        source_span_text: str,
    ) -> str:
        """Write the evolution into canon (§8.5): re-assert the fact from this beat.

        When the contradicting state is identifiable, re-assert its
        ``(subject, predicate, object)`` from the current beat (e.g. "sword
        reacquired"). Otherwise record a typed ``canon_evolved`` fact carrying the
        claim — either way a real, versioned ``assert_state`` write.
        """
        cited = self._find_state(canon_slice, conflict.contradicting_state_id)
        source_span = {"page": 0, "note": source_span_text[:200]} if source_span_text else None
        if cited is not None:
            return await self._canon.assert_state(
                book_id=book_id,
                subject_entity_key=cited.subject_entity_key,
                predicate=cited.predicate,
                object_value=cited.object_value,
                valid_from_beat=at_beat,
                source_span=source_span,
            )
        subject = canon_slice.characters[0].entity_key if canon_slice.characters else "story"
        return await self._canon.assert_state(
            book_id=book_id,
            subject_entity_key=subject,
            predicate="canon_evolved",
            object_value=conflict.claim,
            valid_from_beat=at_beat,
            source_span=source_span,
        )

    @staticmethod
    def _find_state(canon_slice: CanonSlice, state_id: str | None) -> StateSlice | None:
        if state_id is None:
            return None
        return next((s for s in canon_slice.active_states if s.state_id == state_id), None)


# --------------------------------------------------------------------------- #
# Evolve-canon propagation (§8.5): when canon evolves, the change ripples.
# --------------------------------------------------------------------------- #


class RetirementProposal(BaseModel):
    """One cascading retirement the canon-write layer should apply on an evolve.

    ``evolve_canon`` does not just assert the new fact — the prior, now-superseded
    fact on the same functional channel (and any fact that depended on a now-gone
    object) must be retired so the §8.4 active set stays single-valued going
    forward. This is the §8.5 "close the old fact's interval" write, *derived* by
    the pure reasoner and surfaced for the canon service to apply.
    """

    model_config = ConfigDict(extra="forbid")

    state_id: str
    retire_at_beat: int
    reason: str
    proof: str = ""


def propagate_evolution(
    canon_slice: CanonSlice,
    *,
    subject_entity_key: str,
    predicate: str,
    object_value: str,
    at_beat: int,
) -> list[RetirementProposal]:
    """Derive the cascading retirements an ``evolve_canon`` should also apply (§8.5).

    Builds a :class:`CanonTimeline` from the slice's active states, then asks the
    pure propagation reasoner which prior facts the newly-asserted fact
    supersedes. Returns typed proposals (with proof traces) for the canon-write
    seam — it stays the *caller's* decision whether to apply them, mirroring how
    §8.5 keeps the stale fact for time-travel reads.
    """
    from app.render.continuity_reasoning import BeatInterval, CanonTimeline
    from app.render.continuity_reasoning.facts import Fact, fact_slot
    from app.render.continuity_reasoning.propagation import propagate_supersede

    timeline = CanonTimeline.from_state_slices(list(canon_slice.active_states))
    new_fact = Fact(
        subject=subject_entity_key,
        predicate=predicate,
        object=object_value,
        interval=BeatInterval(at_beat, None),
        fact_id="evolved",
        slot=fact_slot(predicate, object_value),
    )
    proposals: list[RetirementProposal] = []
    for effect in propagate_supersede(timeline, new_fact=new_fact):
        if not effect.affected.fact_id:
            continue
        proposals.append(
            RetirementProposal(
                state_id=effect.affected.fact_id,
                retire_at_beat=effect.at_beat,
                reason=effect.trace.summary,
                proof=effect.trace.render(),
            )
        )
    return proposals


__all__ = [
    "Arbiter",
    "CanonEvolver",
    "ConflictResolution",
    "ConflictResolver",
    "ContinuityChecker",
    "ResolutionAction",
    "RetirementProposal",
    "propagate_evolution",
]
