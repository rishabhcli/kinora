"""Propagation of state changes across dependent facts (§8.5).

"Timely forgetting" (§8.5) is not just closing one fact's interval — a state
change ripples. When the sword is lost (``possesses[weapon]`` retired at beat
34), any fact that *depended* on the hero having the sword is now suspect: a
later ``possesses`` re-assert is fine, but a standing ``wielding`` /
``threatens_with`` that references the lost prop should be reviewed, and a prop
that is *destroyed* invalidates every entity's possession of it.

This module computes those ripples **purely**. Given a retirement (or a new
asserted fact that supersedes a functional channel), it walks the timeline's
entity/object indices and returns the :class:`PropagationEffect`s — facts that
must be retired, reviewed, or that are newly orphaned — each with a proof trace.
It performs no writes; the agent/canon layer decides what to apply (§8.5 keeps
the stale fact for time-travel reads, so propagation *proposes* closures).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .facts import Fact
from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline


class EffectKind(StrEnum):
    """What propagation recommends for a dependent fact."""

    #: The dependent fact references a now-gone object; close it at the same beat.
    RETIRE = "retire"
    #: The dependent fact may still hold but needs a model/QA review.
    REVIEW = "review"
    #: A fact is left dangling (its object entity no longer exists / is destroyed).
    ORPHANED = "orphaned"


@dataclass(frozen=True, slots=True)
class PropagationEffect:
    """One downstream consequence of a state change, with its recommendation."""

    kind: EffectKind
    affected: Fact
    cause: Fact
    at_beat: int
    trace: ProofTrace


def propagate_retirement(
    timeline: CanonTimeline,
    *,
    retired_object: str,
    at_beat: int,
    cause: Fact | None = None,
) -> list[PropagationEffect]:
    """Ripple a retirement: what depends on ``retired_object`` after ``at_beat``?

    ``retired_object`` is the entity/prop that ceased to be possessed/present
    (e.g. ``prop_sword_001``). Any fact that *mentions* it as an object and is
    still active past ``at_beat`` is a dependent. Possession-style predicates
    are recommended for ``RETIRE`` (the link is broken); other references are
    recommended for ``REVIEW``. The triggering ``cause`` fact (if known) is
    cited in each trace.
    """
    effects: list[PropagationEffect] = []
    cause_label = cause.label() if cause is not None else f"'{retired_object}' retired"
    for fact in timeline.facts:
        if fact.object != retired_object:
            continue
        if not _active_after(fact, at_beat):
            continue
        kind = EffectKind.RETIRE if fact.is_entity_valued else EffectKind.REVIEW
        verb = "must be closed" if kind is EffectKind.RETIRE else "should be reviewed"
        steps = (
            ProofStep(
                rule=Rule.TRANSITIVE,
                premises=(cause_label, f"dependent: {fact.label()}"),
                conclusion=(
                    f"'{retired_object}' is gone after beat {at_beat}; "
                    f"{fact.subject}.{fact.predicate} references it"
                ),
            ),
            ProofStep(
                rule=Rule.TRANSITIVE,
                premises=(f"{fact.predicate} is entity-valued: {fact.is_entity_valued}",),
                conclusion=f"dependent fact {verb} from beat {at_beat}",
            ),
        )
        effects.append(
            PropagationEffect(
                kind=kind,
                affected=fact,
                cause=cause if cause is not None else fact,
                at_beat=at_beat,
                trace=ProofTrace(
                    summary=(
                        f"{fact.subject}.{fact.predicate} depends on retired "
                        f"'{retired_object}'"
                    ),
                    steps=steps,
                    contradiction=False,
                    cited_fact_ids=tuple(fid for fid in (fact.fact_id,) if fid),
                ),
            )
        )
    effects.sort(key=lambda e: (e.affected.interval.start, e.affected.subject))
    return effects


def propagate_supersede(
    timeline: CanonTimeline, *, new_fact: Fact
) -> list[PropagationEffect]:
    """A newly-asserted functional fact supersedes earlier values on its channel.

    Returns ``RETIRE`` effects for every still-open fact on the same channel that
    holds a *different* object and started before ``new_fact`` — they should be
    closed at ``new_fact``'s start beat (the §8.5 "retire the old fact" write
    that keeps the channel single-valued going forward).
    """
    effects: list[PropagationEffect] = []
    at_beat = new_fact.interval.start
    for fact in timeline.channel_history(*new_fact.channel):
        if fact.fact_id and fact.fact_id == new_fact.fact_id:
            continue
        if fact.object == new_fact.object:
            continue
        if fact.interval.start >= at_beat:
            continue
        if fact.interval.end is not None and fact.interval.end <= at_beat:
            continue  # already closed before the new fact
        steps = (
            ProofStep(
                rule=Rule.FUNCTIONAL_CONFLICT,
                premises=(f"new: {new_fact.label()}", f"prior open: {fact.label()}"),
                conclusion=(
                    f"channel {new_fact.subject}.{new_fact.predicate} is functional; "
                    f"the prior value '{fact.object}' cannot coexist from beat {at_beat}"
                ),
            ),
            ProofStep(
                rule=Rule.FUNCTIONAL_CONFLICT,
                premises=(f"new value '{new_fact.object}' begins at beat {at_beat}",),
                conclusion=f"retire prior fact at beat {at_beat} (§8.5)",
            ),
        )
        effects.append(
            PropagationEffect(
                kind=EffectKind.RETIRE,
                affected=fact,
                cause=new_fact,
                at_beat=at_beat,
                trace=ProofTrace(
                    summary=f"'{new_fact.object}' supersedes '{fact.object}' from beat {at_beat}",
                    steps=steps,
                    contradiction=False,
                    cited_fact_ids=tuple(fid for fid in (fact.fact_id,) if fid),
                ),
            )
        )
    return effects


def _active_after(fact: Fact, beat: int) -> bool:
    """``True`` if the fact is still in force at any beat strictly after ``beat``."""
    if fact.interval.is_open:
        return True
    return fact.interval.end is not None and fact.interval.end > beat + 1


__all__ = [
    "EffectKind",
    "PropagationEffect",
    "propagate_retirement",
    "propagate_supersede",
]
