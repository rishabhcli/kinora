"""Deterministic conflict-resolution engine for contradictory canon proposals.

When two agents propose contradictory canon facts about the same
``(subject, predicate)`` — the Cinematographer wants the hero holding a sword, the
Continuity Supervisor knows it was lost in the river (kinora.md §7.2) — canon2
resolves the dispute under a **fixed, deterministic policy**, records full
provenance for both sides, and queues anything it cannot auto-resolve for
arbitration. This mirrors the §7.2 Showrunner negotiation protocol:

    if a side has textual support and the other doesn't  -> evolve  (grounded wins)
    elif the predicate is user-facing / both grounded    -> flag    (surface_to_user)
    else                                                 -> honor   (LWW by stamp)

The engine is **pure** — it takes two proposals + a policy and returns a
:class:`Resolution`; the *queue* of flagged conflicts is held by the store
(:mod:`.store`). No DB, no network, fully deterministic in tests.

Why deterministic LWW as the floor: the canon must converge to the *same* truth
on every replica regardless of which proposal arrived first (the CRDT invariant
of :mod:`app.memory.crdt`). We reuse :class:`~app.memory.crdt.Stamp` as the total
order so canon2's tiebreak is identical to the bitemporal engine's.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.memory.bitemporal import utcnow
from app.memory.crdt import HLC, Stamp


class ConflictPolicy(StrEnum):
    """The deterministic arbitration outcome (§7.2 vocabulary)."""

    #: One proposal has textual support the other lacks → the grounded one wins,
    #: the canon *evolves* (the story genuinely changed). §7.2 ``evolve_canon``.
    EVOLVE = "evolve"
    #: Cannot be auto-resolved (both grounded, or a user-facing predicate) → queue
    #: for the Showrunner / director. §7.2 ``surface_to_user``.
    FLAG = "flag"
    #: Safe default: respect the established truth via last-writer-wins by stamp.
    #: §7.2 ``honor_canon``.
    HONOR = "honor"


#: Predicates whose contradiction always goes to the director rather than being
#: auto-resolved — story-defining facts where a silent overwrite would be wrong.
USER_FACING_PREDICATES: frozenset[str] = frozenset(
    {"status", "alive", "located_at", "identity", "relationship", "allegiance"}
)


class Proposal(BaseModel):
    """One agent's proposed canon fact, with the provenance the policy weighs."""

    subject: str
    predicate: str
    object_value: str
    actor_id: str = "system"
    #: Monotone wall-ms for the proposal's stamp (deterministic in tests).
    wall_ms: int = 0
    counter: int = 0
    #: The source span grounding the claim (page / char-range). Presence of a span
    #: is what "textual support" means in the §7.2 policy.
    source_span: dict[str, Any] | None = None
    #: An explicit "the director asked for this" flag — forces a non-honor path.
    user_directed: bool = False
    reason: str | None = None

    def stamp(self) -> Stamp:
        """The total-order write stamp (LWW tiebreak), reusing the CRDT clock."""
        return Stamp(HLC(self.wall_ms, self.counter), self.actor_id)

    @property
    def grounded(self) -> bool:
        """True iff the proposal cites a source span (the §7.2 'textual support')."""
        return bool(self.source_span)


class Resolution(BaseModel):
    """The decision record for a resolved (or flagged) conflict (§7.2)."""

    subject: str
    predicate: str
    policy: ConflictPolicy
    #: The object value that should become canon (``None`` only when flagged and
    #: deliberately left undecided).
    winning_object: str | None = None
    #: "incoming" | "existing" | None — which proposal won (None when flagged).
    winner: str | None = None
    incoming_object: str
    existing_object: str
    reason: str
    decided_at: datetime = Field(default_factory=utcnow)
    #: Set when ``policy == FLAG``: the queued conflict's id.
    conflict_id: str | None = None


class FlaggedConflict(BaseModel):
    """A conflict the engine could not auto-resolve — queued for arbitration.

    Shaped to mirror the §7.2 structured conflict object so the existing
    conflict-log / director route could consume it: id, the two claims, who
    raised each, and the options the Showrunner picks among.
    """

    conflict_id: str
    book_id: str
    branch: str = "main"
    subject: str
    predicate: str
    incoming: Proposal
    existing: Proposal
    current_beat: int | None = None
    options: list[dict[str, Any]] = Field(default_factory=list)
    resolved: bool = False
    chosen_object: str | None = None
    resolved_by: str | None = None
    reasoning: str | None = None
    raised_at: datetime = Field(default_factory=utcnow)


def resolve(
    incoming: Proposal,
    existing: Proposal,
    *,
    user_facing_predicates: frozenset[str] = USER_FACING_PREDICATES,
) -> Resolution:
    """Apply the §7.2 deterministic arbitration policy to two contradicting proposals.

    Precondition: ``incoming`` and ``existing`` share ``(subject, predicate)`` but
    differ in ``object_value`` (a genuine contradiction). If they agree, the caller
    should not invoke this — but we still return a HONOR no-op for safety.
    """
    subject, predicate = incoming.subject, incoming.predicate
    inc_obj, exi_obj = incoming.object_value, existing.object_value

    if inc_obj == exi_obj:
        return Resolution(
            subject=subject,
            predicate=predicate,
            policy=ConflictPolicy.HONOR,
            winning_object=exi_obj,
            winner="existing",
            incoming_object=inc_obj,
            existing_object=exi_obj,
            reason="no contradiction: both proposals agree",
        )

    # 1) surface_to_user (highest priority) — a story-defining (user-facing)
    #    predicate, or an explicit director ask, is too load-bearing to evolve or
    #    overwrite silently. It always goes to arbitration, even if one side is
    #    grounded — the §7.2 demo conflict (a 'status' flip) is exactly this case.
    if predicate in user_facing_predicates or incoming.user_directed:
        why = (
            "user-directed edit needs confirmation"
            if incoming.user_directed
            else f"'{predicate}' is a story-defining (user-facing) predicate"
        )
        return _flag(subject, predicate, inc_obj, exi_obj, why)

    # 2) evolve_canon — exactly one side carries textual support → it wins; the
    #    canon genuinely changed (a grounded vs ungrounded pair is the clean
    #    evolve case for a non-user-facing predicate).
    if incoming.grounded and not existing.grounded:
        return _decide(
            ConflictPolicy.EVOLVE, incoming, existing, "incoming", subject, predicate,
            "incoming proposal has textual support the existing fact lacks (evolve_canon)",
        )
    if existing.grounded and not incoming.grounded:
        return _decide(
            ConflictPolicy.EVOLVE, incoming, existing, "existing", subject, predicate,
            "existing fact has textual support the incoming proposal lacks (evolve_canon)",
        )

    # 3) surface_to_user — both sides grounded is a genuine story ambiguity the
    #    auto-policy can't adjudicate → flag it for arbitration.
    if incoming.grounded and existing.grounded:
        return _flag(
            subject, predicate, inc_obj, exi_obj,
            "both proposals are grounded — genuine ambiguity",
        )

    # 4) honor_canon — neither grounded, not user-facing → deterministic LWW.
    winner = "incoming" if incoming.stamp().dominates(existing.stamp()) else "existing"
    return _decide(
        ConflictPolicy.HONOR, incoming, existing, winner, subject, predicate,
        "honor_canon: deterministic last-writer-wins by CRDT stamp",
    )


def build_options(incoming: Proposal, existing: Proposal) -> list[dict[str, Any]]:
    """The §7.2 option list a flagged conflict offers the Showrunner / director."""
    exi = f"{existing.subject} {existing.predicate} = {existing.object_value}"
    inc = f"{incoming.subject} {incoming.predicate} = {incoming.object_value}"
    return [
        {
            "id": "honor_canon",
            "action": f"keep existing: {exi}",
            "object": existing.object_value,
        },
        {
            "id": "evolve_canon",
            "action": f"adopt incoming: {inc}",
            "object": incoming.object_value,
            "requires": "textual support",
        },
        {
            "id": "surface_to_user",
            "action": "ask the director to choose",
            "object": None,
        },
    ]


def _flag(
    subject: str, predicate: str, inc_obj: str, exi_obj: str, why: str
) -> Resolution:
    """Build a FLAG resolution (the §7.2 ``surface_to_user`` decision record)."""
    return Resolution(
        subject=subject,
        predicate=predicate,
        policy=ConflictPolicy.FLAG,
        winning_object=None,
        winner=None,
        incoming_object=inc_obj,
        existing_object=exi_obj,
        reason=f"surface_to_user: {why}",
    )


def _decide(
    policy: ConflictPolicy,
    incoming: Proposal,
    existing: Proposal,
    winner: str,
    subject: str,
    predicate: str,
    reason: str,
) -> Resolution:
    won = incoming if winner == "incoming" else existing
    return Resolution(
        subject=subject,
        predicate=predicate,
        policy=policy,
        winning_object=won.object_value,
        winner=winner,
        incoming_object=incoming.object_value,
        existing_object=existing.object_value,
        reason=reason,
    )


__all__ = [
    "USER_FACING_PREDICATES",
    "ConflictPolicy",
    "FlaggedConflict",
    "Proposal",
    "Resolution",
    "build_options",
    "resolve",
]
