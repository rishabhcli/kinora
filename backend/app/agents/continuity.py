"""Continuity Supervisor — guards the canon, raises structured conflicts (§7.2, §8.5).

Given a proposed shot and the active continuity facts at the current beat, the
Supervisor decides whether the depiction contradicts the canon (e.g. a shot
draws a sword that was retired at an earlier beat). There are two judgement
paths, and both end in the same deterministic, typed :class:`ConflictObject`
(§7.2) with the fixed honor/surface/evolve options the Showrunner arbitrates:

* the **legacy single-call judgement** (:meth:`Continuity.check_shot`): one
  reasoning-model call returns a :class:`ContinuityJudgment`; the conflict
  construction is deterministic. Kept for back-compat and as a fallback.

* the **formal reasoning path** (:meth:`Continuity.check_shot_formal`): the model
  does only the part it is good at — turning shot prose into a list of *implied
  facts* (:class:`ContinuityClaims`) — and the pure
  :class:`~app.render.continuity_reasoning.ContinuityEngine` then *derives*
  contradictions over the versioned canon using Allen interval algebra (§8.5),
  emitting a human-readable PROOF TRACE for each. The conflict carries that
  proof in its ``claim``/``canon_fact``, so §7.2 stays inspectable and the
  verdict is reproducible without a model in the loop.

The temporal/contradiction/epistemic reasoning lives in
:mod:`app.render.continuity_reasoning` (pure, exhaustively unit-tested); this
agent is the thin model-bound shell that feeds it and renders its verdict.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.config import Settings, get_settings
from app.memory.interfaces import CanonSlice, StateSlice
from app.providers import Providers
from app.render.continuity_reasoning import (
    ContinuityEngine,
    ContinuityVerdict,
    FactQuery,
    fact_slot,
)
from app.render.continuity_reasoning.engine import ContinuityFinding, FindingKind

from .base import BaseAgent
from .contracts import (
    ConflictObject,
    ConflictOption,
    ConflictOptionSpec,
    ConflictType,
    ContinuityResult,
    ShotSpec,
)
from .prompts import CONTINUITY

#: Map an engine finding kind to the §7.2 conflict type it raises.
_FINDING_TO_CONFLICT_TYPE: dict[FindingKind, ConflictType] = {
    FindingKind.CONTRADICTION: ConflictType.CANON_VIOLATION,
    FindingKind.SPATIAL: ConflictType.CANON_VIOLATION,
    FindingKind.SPOILER: ConflictType.TIMELINE_CONTRADICTION,
    FindingKind.TEMPORAL: ConflictType.TIMELINE_CONTRADICTION,
}


class ContinuityJudgment(BaseModel):
    """The reasoning model's raw verdict on a proposed depiction (internal)."""

    model_config = ConfigDict(extra="ignore")

    contradicts: bool = False
    contradicting_state_id: str | None = None
    claim: str = ""
    canon_fact: str | None = None
    reasoning: str = ""


class ImpliedFact(BaseModel):
    """One fact a proposed shot *asserts*, extracted from its prose (formal path).

    The model's only job on the formal path is this extraction: "drawing a sword"
    ⇒ ``subject=char_hero predicate=possesses object=sword``. The pure engine
    then proves each implied fact against the versioned canon — the model never
    judges the contradiction itself, so the verdict is deterministic and the
    proof is reproducible.
    """

    model_config = ConfigDict(extra="ignore")

    subject_entity_key: str
    predicate: str
    object_value: str
    #: Optional explicit functional slot; derived from the object when blank.
    slot: str = ""


class ContinuityClaims(BaseModel):
    """The model's extraction of every fact a proposed shot implies (formal path)."""

    model_config = ConfigDict(extra="ignore")

    implied_facts: list[ImpliedFact] = Field(default_factory=list)


def build_conflict(
    judgment: ContinuityJudgment,
    *,
    shot_id: str | None,
    current_beat: str | None,
    active_states: list[StateSlice],
    target_duration_s: float = 5.0,
) -> ConflictObject:
    """Construct the §7.2 conflict object from a contradiction judgment (deterministic)."""
    cited = next(
        (s for s in active_states if s.state_id == judgment.contradicting_state_id), None
    )
    canon_fact = judgment.canon_fact or (format_state(cited) if cited else None)
    return ConflictObject(
        conflict_id=f"cf_{shot_id or current_beat or 'unknown'}",
        raised_by="continuity_supervisor",
        type=ConflictType.CANON_VIOLATION,
        shot_id=shot_id,
        claim=judgment.claim or "the proposed shot contradicts the established canon",
        canon_fact=canon_fact,
        current_beat=current_beat,
        contradicting_state_id=judgment.contradicting_state_id,
        user_facing=True,
        options=_conflict_options(target_duration_s),
    )


def build_conflict_from_finding(
    finding: ContinuityFinding,
    *,
    shot_id: str | None,
    current_beat: str | None,
    target_duration_s: float = 5.0,
) -> ConflictObject:
    """Construct a §7.2 conflict from an engine finding + its PROOF TRACE.

    The proof trace is rendered into ``canon_fact`` so the conflict object — and
    the agent-activity feed that shows it (§7.2, §13) — carries the *derivation*,
    not just the verdict. ``contradicting_state_id`` points at the cited canon
    fact so the UI can highlight the offending node and ``evolve_canon`` can
    re-assert exactly that fact.
    """
    return ConflictObject(
        conflict_id=f"cf_{shot_id or current_beat or 'unknown'}",
        raised_by="continuity_supervisor",
        type=_FINDING_TO_CONFLICT_TYPE.get(finding.kind, ConflictType.CANON_VIOLATION),
        shot_id=shot_id,
        claim=finding.summary,
        canon_fact=finding.trace.render(),
        current_beat=current_beat,
        contradicting_state_id=finding.cited_fact_id,
        # A spoiler is never user-facing as an evolve choice; it is a hard block.
        user_facing=finding.kind is not FindingKind.SPOILER,
        options=_conflict_options(target_duration_s),
    )


def _conflict_options(target_duration_s: float) -> list[ConflictOptionSpec]:
    """The fixed three §7.2 options (honor / surface / evolve)."""
    return [
        ConflictOptionSpec(
            id=ConflictOption.HONOR_CANON,
            action="regenerate the shot honouring the established canon",
            cost_video_s=target_duration_s,
        ),
        ConflictOptionSpec(
            id=ConflictOption.SURFACE_TO_USER,
            action="ask the director to choose",
            cost_video_s=0.0,
        ),
        ConflictOptionSpec(
            id=ConflictOption.EVOLVE_CANON,
            action="assert the new state and regenerate",
            requires="textual support",
        ),
    ]


def format_state(state: StateSlice) -> str:
    """Render an active continuity fact as a human-readable canon fact string."""
    interval = f"valid from beat {state.valid_from_beat}"
    if state.valid_to_beat is not None:
        interval += f" to {state.valid_to_beat}"
    return (
        f"{state.state_id}: {state.subject_entity_key} {state.predicate} "
        f"{state.object_value} ({interval})"
    )


class Continuity(BaseAgent):
    """Detects canon violations in a proposed shot and raises a typed conflict."""

    def __init__(
        self,
        providers: Providers,
        *,
        settings: Settings | None = None,
        skills: object | None = None,
    ) -> None:
        settings = settings or get_settings()
        super().__init__(
            providers,
            name="continuity_supervisor",
            model=settings.chat_model_plus,
            prompt=CONTINUITY,
            skills=skills,  # type: ignore[arg-type]
        )

    async def check_shot(
        self,
        proposed: ShotSpec | str,
        canon_slice: CanonSlice,
        *,
        shot_id: str | None = None,
        current_beat_id: str | None = None,
        target_duration_s: float = 5.0,
    ) -> ContinuityResult:
        """Return a clean result, or a structured conflict on a canon violation.

        The legacy single-call path: one reasoning-model judgement, deterministic
        conflict construction.
        """
        depiction, resolved_shot_id = self._depiction(proposed, shot_id)
        current_beat = current_beat_id or canon_slice.beat_id
        payload = {
            "proposed_depiction": depiction,
            "current_beat": current_beat,
            "active_states": [s.model_dump(mode="json") for s in canon_slice.active_states],
        }
        judgment = await self.run_json(payload, ContinuityJudgment, temperature=0.0)
        if not judgment.contradicts:
            return ContinuityResult(ok=True, conflict=None)
        conflict = build_conflict(
            judgment,
            shot_id=resolved_shot_id,
            current_beat=current_beat,
            active_states=list(canon_slice.active_states),
            target_duration_s=target_duration_s,
        )
        return ContinuityResult(ok=False, conflict=conflict)

    async def check_shot_formal(
        self,
        proposed: ShotSpec | str,
        canon_slice: CanonSlice,
        *,
        shot_id: str | None = None,
        current_beat_id: str | None = None,
        target_duration_s: float = 5.0,
        hidden_state_ids: list[str] | None = None,
        revealed_at: dict[str, int] | None = None,
        use_inference: bool = True,
        check_spoilers: bool = True,
    ) -> ContinuityResult:
        """Formal path: the model extracts implied facts, the engine proves them (§8.5).

        Pulls the *implied facts* out of the depiction with a single extraction
        call, then runs the pure :class:`ContinuityEngine` over the canon slice's
        versioned states. The first derived contradiction (or spoiler) becomes a
        §7.2 conflict whose ``canon_fact`` is the full PROOF TRACE; a clean
        verdict approves the shot.
        """
        depiction, resolved_shot_id = self._depiction(proposed, shot_id)
        current_beat = current_beat_id or canon_slice.beat_id
        claims = await self._extract_claims(depiction, canon_slice)
        queries = self._claims_to_queries(claims, canon_slice.beat_index)
        if not queries:
            # Nothing concrete to test; defer to the legacy judgement so a vague
            # depiction is not silently approved.
            return await self.check_shot(
                proposed,
                canon_slice,
                shot_id=shot_id,
                current_beat_id=current_beat_id,
                target_duration_s=target_duration_s,
            )
        verdict = run_engine_verdict(
            canon_slice,
            queries,
            beat_index=canon_slice.beat_index,
            hidden_state_ids=hidden_state_ids or [],
            revealed_at=revealed_at,
            use_inference=use_inference,
            check_spoilers=check_spoilers,
        )
        if verdict.ok or verdict.primary is None:
            return ContinuityResult(ok=True, conflict=None)
        conflict = build_conflict_from_finding(
            verdict.primary,
            shot_id=resolved_shot_id,
            current_beat=current_beat,
            target_duration_s=target_duration_s,
        )
        return ContinuityResult(ok=False, conflict=conflict)

    async def _extract_claims(
        self, depiction: str, canon_slice: CanonSlice
    ) -> ContinuityClaims:
        """Ask the model to extract the facts the depiction implies (formal path)."""
        payload = {
            "task": "extract_implied_facts",
            "proposed_depiction": depiction,
            "known_entities": [c.entity_key for c in canon_slice.characters]
            + ([canon_slice.location.entity_key] if canon_slice.location else []),
            "active_states": [s.model_dump(mode="json") for s in canon_slice.active_states],
        }
        return await self.run_json(payload, ContinuityClaims, temperature=0.0)

    @staticmethod
    def _claims_to_queries(claims: ContinuityClaims, beat_index: int) -> list[FactQuery]:
        """Turn the model's implied facts into engine fact queries at this beat."""
        queries: list[FactQuery] = []
        for fact in claims.implied_facts:
            slot = fact.slot or fact_slot(fact.predicate, fact.object_value)
            queries.append(
                FactQuery(
                    subject=fact.subject_entity_key,
                    predicate=fact.predicate,
                    object=fact.object_value,
                    at_beat=beat_index,
                    slot=slot,
                )
            )
        return queries

    @staticmethod
    def _depiction(proposed: ShotSpec | str, shot_id: str | None) -> tuple[str, str | None]:
        if isinstance(proposed, ShotSpec):
            text = proposed.prompt or (proposed.beat_id or "")
            return text, proposed.shot_id or shot_id
        return proposed, shot_id


def run_engine_verdict(
    canon_slice: CanonSlice,
    queries: list[FactQuery],
    *,
    beat_index: int,
    hidden_state_ids: list[str] | None = None,
    revealed_at: dict[str, int] | None = None,
    use_inference: bool = True,
    check_spoilers: bool = True,
) -> ContinuityVerdict:
    """Run the pure :class:`ContinuityEngine` over a canon slice (no model call).

    Exposed as a free function so the conflict-resolution wiring and tests can
    drive the deterministic reasoning directly, with the implied-fact queries
    supplied externally (e.g. injected to exercise a branch without the model).
    """
    engine = ContinuityEngine.from_state_slices(
        list(canon_slice.active_states),
        hidden_state_ids=hidden_state_ids or [],
        revealed_at=revealed_at,
    )
    if use_inference:
        engine = engine.with_inference_at(beat_index)
    return engine.check_shot_claims(queries, check_spoilers=check_spoilers)


__all__ = [
    "Continuity",
    "ContinuityClaims",
    "ContinuityJudgment",
    "ImpliedFact",
    "build_conflict",
    "build_conflict_from_finding",
    "format_state",
    "run_engine_verdict",
]
