"""Epistemic tracking: what the reader KNOWS vs. what is canonically true (§10).

The canon graph holds *ground truth*; a film generated ahead of the reader must
also respect what the reader has *learned* so far, or it spoils. §10's Critic
guardrail — "do not be charitable; a wrong face is a fail" — extends to the
narrative: depicting a yet-to-be-revealed twist is as wrong as a wrong face.

This module separates two knowledge frontiers at a beat:

* **Canon-true set** — every fact active at the beat (ground truth).
* **Reader-known set** — the subset the reader has been shown/told by the beat
  (a ``HIDDEN`` fact only enters once its ``revealed_at_beat`` passes).

From the gap it derives:

* :class:`SpoilerRisk` — a fact that is canon-true *and active* but reader-
  unknown at the beat; depicting it would reveal it early.
* dramatic-irony detection — a reader holding a ``MISTAKEN`` belief the canon
  contradicts.

All pure: it reads a :class:`~.timeline.CanonTimeline` and returns value objects.
"""

from __future__ import annotations

from dataclasses import dataclass

from .facts import Fact, FactQuery, Visibility
from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline


@dataclass(frozen=True, slots=True)
class EpistemicReport:
    """The reader's knowledge frontier at a beat, split from canon truth."""

    beat: int
    canon_true: tuple[Fact, ...]
    reader_known: tuple[Fact, ...]
    #: Canon-true & active but not yet reader-known (the dramatic-irony set).
    reader_unaware: tuple[Fact, ...]

    @property
    def has_dramatic_irony(self) -> bool:
        """``True`` if the reader is unaware of any active canon fact."""
        return bool(self.reader_unaware)


@dataclass(frozen=True, slots=True)
class SpoilerRisk:
    """A depiction that would reveal a fact the reader does not yet know."""

    fact: Fact
    beat: int
    trace: ProofTrace


def reader_knowledge_at(timeline: CanonTimeline, beat: int) -> EpistemicReport:
    """Split the canon at ``beat`` into canon-true vs. reader-known (§10)."""
    canon_true = timeline.active_at(beat)
    known = tuple(f for f in canon_true if f.known_to_reader_at(beat))
    unaware = tuple(
        f
        for f in canon_true
        if f.visibility is Visibility.HIDDEN and not f.known_to_reader_at(beat)
    )
    return EpistemicReport(
        beat=beat, canon_true=canon_true, reader_known=known, reader_unaware=unaware
    )


def spoiler_risks(timeline: CanonTimeline, beat: int) -> list[SpoilerRisk]:
    """Every active fact the reader does not yet know at ``beat`` (spoiler set).

    The Cinematographer must not depict these; the Continuity Supervisor surfaces
    them so a shot at beat N cannot leak a reveal scheduled for beat N+k.
    """
    report = reader_knowledge_at(timeline, beat)
    out: list[SpoilerRisk] = []
    for fact in report.reader_unaware:
        reveal = fact.revealed_at_beat
        steps = (
            ProofStep(
                rule=Rule.EPISTEMIC_SPOILER,
                premises=(
                    f"canon-true & active: {fact.label()}",
                    f"reader learns it only at beat {reveal}",
                ),
                conclusion=(
                    f"at beat {beat} < {reveal} the reader does NOT know this fact"
                ),
            ),
            ProofStep(
                rule=Rule.EPISTEMIC_SPOILER,
                premises=(f"beat {beat} is before reveal {reveal}",),
                conclusion=(
                    f"SPOILER: depicting '{fact.object}' at beat {beat} reveals it early"
                ),
            ),
        )
        out.append(
            SpoilerRisk(
                fact=fact,
                beat=beat,
                trace=ProofTrace(
                    summary=(
                        f"depicting {fact.subject}.{fact.predicate} spoils a "
                        f"beat-{reveal} reveal"
                    ),
                    steps=steps,
                    contradiction=True,
                    cited_fact_ids=(fact.fact_id,) if fact.fact_id else (),
                ),
            )
        )
    return out


def check_spoiler(timeline: CanonTimeline, query: FactQuery) -> SpoilerRisk | None:
    """Would depicting ``query`` reveal a reader-unknown fact at its beat?

    The live counterpart of :func:`spoiler_risks`: tests one proposed depiction
    against the reader's frontier. Returns the risk if the proposed value matches
    a canon-true fact the reader has not yet learned.
    """
    for risk in spoiler_risks(timeline, query.at_beat):
        f = risk.fact
        if (
            f.subject == query.subject
            and f.predicate == query.predicate
            and f.object == query.object
        ):
            return risk
    return None


def dramatic_irony_beats(timeline: CanonTimeline) -> list[int]:
    """The beats at which the reader is unaware of some active canon fact.

    Useful for an authoring/debug view: which stretches of the film carry
    dramatic irony (reader knows less than the canon). Scans the union of fact
    start beats — irony only changes at fact/reveal boundaries.
    """
    boundaries: set[int] = set()
    for fact in timeline.facts:
        boundaries.add(fact.interval.start)
        if fact.revealed_at_beat is not None:
            boundaries.add(fact.revealed_at_beat)
    out = [b for b in sorted(boundaries) if reader_knowledge_at(timeline, b).has_dramatic_irony]
    return out


__all__ = [
    "EpistemicReport",
    "SpoilerRisk",
    "check_spoiler",
    "dramatic_irony_beats",
    "reader_knowledge_at",
    "spoiler_risks",
]
