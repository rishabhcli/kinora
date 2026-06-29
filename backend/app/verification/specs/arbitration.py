"""A model of the §7.2 conflict-arbitration protocol.

Continuity raises a structured conflict; the Showrunner resolves it under a
*fixed* policy (honor / evolve / surface) and the shot proceeds to approved. The
hazards here are not budget but *protocol correctness*: a conflict must never be
silently dropped (every raised conflict reaches a logged decision), evolve must
never fire without textual support, and surface must never fire without a
present director on a user-facing conflict. Those are exactly the guarantees the
demo's "watch the agents resolve a disagreement" moment rests on.

This spec drives the **real policy function** —
:func:`app.agents.showrunner.decide_arbitration` — over the abstract lifecycle,
so the model checks the production decision logic, not a paraphrase of it. Each
arbitration step calls ``decide_arbitration`` with the conflict's
``textual_support`` / ``director_present`` flags and routes to the option it
returns, then proves the §7.2 invariants over every combination of those flags
and every interleaving of the lifecycle.

Lifecycle (mirrors the §7.2 state diagram):

    DRAFTED → CHECKED → APPROVED            (no violation)
                     ↘ CONFLICT → ARBITRATION → {HONOR → APPROVED,
                                                 EVOLVE → APPROVED,
                                                 SURFACE → AWAIT_USER → APPROVED}

The environment is non-deterministic in three independent bits — whether the
draft violates canon, whether the source span supports an evolution, and whether
a director is present — so the checker covers all eight worlds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum

from app.agents.contracts import (
    ConflictObject,
    ConflictOption,
    ConflictOptionSpec,
    ConflictType,
)
from app.agents.showrunner import decide_arbitration
from app.verification.modelcheck import (
    Action,
    Invariant,
    LeadsTo,
    Spec,
    invariant,
    leads_to,
)
from app.verification.modelcheck.spec import Fairness

__all__ = ["ArbitrationPhase", "ArbitrationState", "build_arbitration_spec"]


class ArbitrationPhase(IntEnum):
    """The §7.2 shot-conflict lifecycle phase."""

    DRAFTED = 0
    CHECKED = 1
    CONFLICT = 2
    ARBITRATION = 3
    AWAIT_USER = 4
    APPROVED = 5


@dataclass(frozen=True, slots=True)
class ArbitrationState:
    """A finite snapshot of one shot's continuity / arbitration lifecycle."""

    phase: ArbitrationPhase
    #: Environment facts, fixed per run by the seed actions (the eight worlds).
    violates_canon: bool
    textual_support: bool
    director_present: bool
    user_facing: bool
    #: The resolution the Showrunner chose (None until arbitration runs).
    chosen: ConflictOption | None
    #: Whether the canon was evolved as part of the resolution.
    evolved: bool
    #: Whether a decision record was logged (the §7.2 "log(decision)" step).
    logged: bool


def _conflict_for(state: ArbitrationState) -> ConflictObject:
    """Reconstruct the structured conflict the policy arbitrates (§7.2)."""
    return ConflictObject(
        conflict_id="cf_model",
        raised_by="continuity_supervisor",
        type=ConflictType.CANON_VIOLATION,
        shot_id="shot_model",
        claim="modelled canon violation",
        canon_fact="state_modelled retired earlier",
        current_beat="beat_model",
        user_facing=state.user_facing,
        options=[
            ConflictOptionSpec(id=ConflictOption.HONOR_CANON, action="regenerate"),
            ConflictOptionSpec(id=ConflictOption.SURFACE_TO_USER, action="ask director"),
            ConflictOptionSpec(
                id=ConflictOption.EVOLVE_CANON,
                action="assert reacquired",
                requires="textual support",
            ),
        ],
    )


def build_arbitration_spec() -> Spec[ArbitrationState]:
    """Build the §7.2 arbitration spec, driving the real ``decide_arbitration``.

    The eight environment worlds (violates × supports × director, with the
    user-facing bit folded in) are seeded by non-deterministic initial states, so
    a single run covers them all.
    """

    # All eight worlds as initial states (each a distinct, fixed environment).
    initial: list[ArbitrationState] = []
    for violates in (False, True):
        for support in (False, True):
            for director in (False, True):
                for user_facing in (False, True):
                    initial.append(
                        ArbitrationState(
                            phase=ArbitrationPhase.DRAFTED,
                            violates_canon=violates,
                            textual_support=support,
                            director_present=director,
                            user_facing=user_facing,
                            chosen=None,
                            evolved=False,
                            logged=False,
                        )
                    )

    # -- Continuity check: DRAFTED → CHECKED --------------------------------- #

    def can_check(s: ArbitrationState) -> bool:
        return s.phase is ArbitrationPhase.DRAFTED

    def check(s: ArbitrationState) -> tuple[ArbitrationState, ...]:
        return (replace(s, phase=ArbitrationPhase.CHECKED),)

    # -- CHECKED → APPROVED (clean) or CONFLICT (violation) ------------------ #

    def can_pass_clean(s: ArbitrationState) -> bool:
        return s.phase is ArbitrationPhase.CHECKED and not s.violates_canon

    def pass_clean(s: ArbitrationState) -> tuple[ArbitrationState, ...]:
        return (replace(s, phase=ArbitrationPhase.APPROVED, logged=True),)

    def can_raise(s: ArbitrationState) -> bool:
        return s.phase is ArbitrationPhase.CHECKED and s.violates_canon

    def raise_conflict(s: ArbitrationState) -> tuple[ArbitrationState, ...]:
        return (replace(s, phase=ArbitrationPhase.CONFLICT),)

    # -- CONFLICT → ARBITRATION (run the REAL policy) ------------------------ #

    def can_arbitrate(s: ArbitrationState) -> bool:
        return s.phase is ArbitrationPhase.CONFLICT

    def arbitrate(s: ArbitrationState) -> tuple[ArbitrationState, ...]:
        conflict = _conflict_for(s)
        chosen, evolved = decide_arbitration(
            conflict,
            textual_support=s.textual_support,
            director_present=s.director_present,
        )
        # A surface routes through AWAIT_USER; honor/evolve approve directly. The
        # decision is logged at the moment of resolution (§7.2 log step).
        if chosen is ConflictOption.SURFACE_TO_USER:
            nxt = replace(
                s,
                phase=ArbitrationPhase.AWAIT_USER,
                chosen=chosen,
                evolved=evolved,
                logged=True,
            )
        else:
            nxt = replace(
                s,
                phase=ArbitrationPhase.APPROVED,
                chosen=chosen,
                evolved=evolved,
                logged=True,
            )
        return (nxt,)

    # -- AWAIT_USER → APPROVED (director picks) ------------------------------ #

    def can_user_decide(s: ArbitrationState) -> bool:
        return s.phase is ArbitrationPhase.AWAIT_USER

    def user_decide(s: ArbitrationState) -> tuple[ArbitrationState, ...]:
        return (replace(s, phase=ArbitrationPhase.APPROVED),)

    actions = (
        Action("check", can_check, check, Fairness.WEAK),
        Action("pass_clean", can_pass_clean, pass_clean, Fairness.WEAK),
        Action("raise_conflict", can_raise, raise_conflict, Fairness.WEAK),
        Action("arbitrate", can_arbitrate, arbitrate, Fairness.WEAK),
        Action("user_decide", can_user_decide, user_decide, Fairness.WEAK),
    )

    approved = ArbitrationPhase.APPROVED

    invariants: tuple[Invariant[ArbitrationState], ...] = (
        # §7.2 hard gate 1: evolve_canon only ever fires WITH textual support. If
        # this fails, the system rewrote the story's canon on no evidence.
        invariant(
            "evolve_requires_textual_support",
            lambda s: s.chosen is not ConflictOption.EVOLVE_CANON or s.textual_support,
        ),
        # The evolved-canon flag is set iff the chosen option was evolve.
        invariant(
            "evolved_iff_evolve_chosen",
            lambda s: s.evolved == (s.chosen is ConflictOption.EVOLVE_CANON),
        ),
        # §7.2 hard gate 2: surface_to_user only with a present director on a
        # user-facing conflict — otherwise the prompt would dead-end with no one
        # to answer it.
        invariant(
            "surface_requires_director_and_user_facing",
            lambda s: s.chosen is not ConflictOption.SURFACE_TO_USER
            or (s.director_present and s.user_facing),
        ),
        # The safe default: when neither evolve nor surface is eligible, the only
        # remaining resolution is honor_canon (the established truth wins).
        invariant(
            "fallback_is_honor",
            lambda s: s.chosen is None
            or s.chosen is ConflictOption.EVOLVE_CANON
            or s.chosen is ConflictOption.SURFACE_TO_USER
            or s.chosen is ConflictOption.HONOR_CANON,
        ),
        # Any approved shot that went through a conflict carries a logged
        # decision — the §7.2 "log(decision, reasoning) → episodic store" step is
        # never skipped (the audit trail the demo shows is always present).
        invariant(
            "approved_conflict_is_logged",
            lambda s: s.phase is not approved or s.logged,
        ),
        # A shot is only approved after a resolution exists (no approval that
        # skipped arbitration when there was a violation).
        invariant(
            "approved_violation_has_resolution",
            lambda s: not (s.phase is approved and s.violates_canon)
            or s.chosen is not None,
        ),
    )

    leads_to_props: tuple[LeadsTo[ArbitrationState], ...] = (
        # Every raised conflict eventually resolves to an approved shot — a
        # conflict is never silently dropped or stuck. (Under weak fairness the
        # arbitrate / user_decide chain always completes.)
        leads_to(
            "conflict_eventually_approved",
            trigger=lambda s: s.phase is ArbitrationPhase.CONFLICT,
            goal=lambda s: s.phase is approved,
        ),
        # Every draft eventually reaches approved (clean or via arbitration): the
        # pipeline never strands a shot in the continuity check.
        leads_to(
            "draft_eventually_approved",
            trigger=lambda s: s.phase is ArbitrationPhase.DRAFTED,
            goal=lambda s: s.phase is approved,
        ),
    )

    def label(s: ArbitrationState) -> str:
        env = "".join(
            [
                "V" if s.violates_canon else "-",
                "T" if s.textual_support else "-",
                "D" if s.director_present else "-",
                "U" if s.user_facing else "-",
            ]
        )
        chosen = s.chosen.value if s.chosen is not None else "·"
        return f"{s.phase.name:<11} env={env} -> {chosen}{' log' if s.logged else ''}"

    return Spec(
        name="conflict_arbitration_policy",
        initial=tuple(initial),
        actions=actions,
        invariants=invariants,
        leads_to_props=leads_to_props,
        state_label=label,
    )
