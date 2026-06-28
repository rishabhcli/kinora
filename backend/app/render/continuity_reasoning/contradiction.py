"""Automatic contradiction detection with proof traces (§7.2, §8.5, §9.5).

This is the formal core of the Continuity Supervisor's judgement. Instead of
asking a model "does this contradict?", the engine *derives* contradictions from
the temporal structure of the canon and a proposed depiction, and emits a
human-readable :class:`~.proof.ProofTrace` for each — the "show your work" the
§7.2 conflict object and the §13 demo need.

Two flavours:

* **Canon self-consistency** (:func:`detect_canon_contradictions`): scan every
  functional channel of a :class:`~.timeline.CanonTimeline` for two facts that
  overlap in time (an Allen overlap relation) but disagree on the object. This
  is the §8.5 invariant — a fact retired at beat 34 must not still be active
  when a newer, different fact is. Catches a canon that was mis-asserted.

* **Proposed-shot check** (:func:`check_proposed_fact`): the live path. A shot
  implies the entity is in some state at the current beat
  (:class:`~.facts.FactQuery`); we test that implied fact against the active
  canon and return a proof trace if it is forbidden — e.g. "draws a sword" at
  beat 39 when ``possesses[weapon]`` was retired at beat 34.

Both are **pure** — they take a timeline + query and return value objects, no
network, no model. The model's only job upstream is to *extract* the implied
:class:`FactQuery` from prose; the contradiction logic is deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass

from .facts import Fact, FactQuery
from .intervals import Allen
from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline


@dataclass(frozen=True, slots=True)
class Contradiction:
    """A derived contradiction between two facts (or a proposed fact + canon)."""

    left: Fact
    right: Fact
    beat: int
    trace: ProofTrace

    @property
    def cited_fact_id(self) -> str:
        """The canon fact id to cite in a §7.2 conflict (prefers the canon side)."""
        # The proposed/point fact uses a synthetic id; prefer a real canon id.
        for fact in (self.right, self.left):
            if fact.fact_id and fact.fact_id != "proposed":
                return fact.fact_id
        return self.left.fact_id or self.right.fact_id


def _overlap_step(a: Fact, b: Fact, relation: Allen) -> ProofStep:
    return ProofStep(
        rule=Rule.TEMPORAL_RELATION,
        premises=(f"{a.label()}", f"{b.label()}"),
        conclusion=(
            f"intervals {a.interval} and {b.interval} stand in Allen "
            f"relation '{relation.value}' ⇒ they share at least one beat"
        ),
    )


def _functional_conflict_trace(a: Fact, b: Fact, beat: int) -> ProofTrace:
    relation = a.interval.relate(b.interval)
    channel = f"{a.subject}.{a.predicate}" + (f"[{a.slot}]" if a.slot else "")
    summary = (
        f"{channel} is functional but holds both '{a.object}' and '{b.object}' "
        f"at beat {beat}"
    )
    steps = (
        ProofStep(
            rule=Rule.FUNCTIONAL_CONFLICT,
            premises=(
                f"channel {channel} is functional (≤1 value per beat)",
                f"both facts disagree: '{a.object}' vs '{b.object}'",
            ),
            conclusion="at most one of the two facts may be active at any beat",
        ),
        _overlap_step(a, b, relation),
        ProofStep(
            rule=Rule.FUNCTIONAL_CONFLICT,
            premises=(f"both active at beat {beat}", "objects differ"),
            conclusion=f"CONTRADICTION at beat {beat}",
        ),
    )
    return ProofTrace(
        summary=summary,
        steps=steps,
        contradiction=True,
        cited_fact_ids=tuple(fid for fid in (a.fact_id, b.fact_id) if fid),
    )


def detect_canon_contradictions(timeline: CanonTimeline) -> list[Contradiction]:
    """Find every functional-channel clash in the canon (self-consistency, §8.5).

    For each functional channel, any two facts whose intervals overlap and whose
    objects differ are a contradiction reported at the first shared beat. Returns
    them ordered by beat then channel for stable output.
    """
    out: list[Contradiction] = []
    for channel in timeline.functional_channels():
        history = timeline.channel_history(*channel)
        for i in range(len(history)):
            for j in range(i + 1, len(history)):
                a, b = history[i], history[j]
                if a.object == b.object:
                    continue
                if not a.interval.overlaps(b.interval):
                    continue
                beat = max(a.interval.start, b.interval.start)
                out.append(
                    Contradiction(
                        left=a, right=b, beat=beat, trace=_functional_conflict_trace(a, b, beat)
                    )
                )
    out.sort(key=lambda c: (c.beat, c.left.subject, c.left.predicate))
    return out


def check_proposed_fact(
    timeline: CanonTimeline, query: FactQuery
) -> Contradiction | None:
    """Test a proposed depiction against the active canon at its beat (live path).

    Returns the first contradiction found (a functional clash with the active
    canon fact on the query's channel, or a depiction of a value that was
    retired before / not yet active at the beat), with a full proof trace.
    ``None`` means the depiction is continuity-clean.
    """
    proposed = query.as_point_fact()
    active = timeline.value_at(query.subject, query.predicate, query.at_beat, query.slot)

    # A functional channel that holds a *different* value right now → clash.
    if active is not None and active.object != query.object:
        relation = proposed.interval.relate(active.interval)
        channel = f"{query.subject}.{query.predicate}" + (
            f"[{query.slot}]" if query.slot else ""
        )
        summary = (
            f"proposed '{query.object}' contradicts canon '{active.object}' on "
            f"{channel} at beat {query.at_beat}"
        )
        steps = (
            ProofStep(
                rule=Rule.PROPOSED_VS_ACTIVE,
                premises=(
                    f"proposed: {query.subject} {query.predicate} {query.object} "
                    f"@ beat {query.at_beat}",
                    f"active canon: {active.label()}",
                ),
                conclusion=f"canon fixes {channel} = '{active.object}' at beat {query.at_beat}",
            ),
            _overlap_step(proposed, active, relation),
            ProofStep(
                rule=Rule.PROPOSED_VS_ACTIVE,
                premises=(f"'{query.object}' ≠ '{active.object}'", "channel is functional"),
                conclusion=f"CONTRADICTION: shot depicts a forbidden state at beat {query.at_beat}",
            ),
        )
        trace = ProofTrace(
            summary=summary,
            steps=steps,
            contradiction=True,
            cited_fact_ids=tuple(fid for fid in (active.fact_id,) if fid),
        )
        return Contradiction(left=proposed, right=active, beat=query.at_beat, trace=trace)

    # No active fact, but the channel *had* this exact value and it was retired
    # before this beat → depicting it now is a continuity error (the §8.5 case).
    if active is None:
        retired = _retired_value(timeline, query)
        if retired is not None:
            return retired
    return None


def _retired_value(timeline: CanonTimeline, query: FactQuery) -> Contradiction | None:
    """Catch "depicts X that was retired before this beat" on a functional channel."""
    history = timeline.channel_history(query.subject, query.predicate, query.slot)
    for fact in history:
        if fact.object != query.object:
            continue
        end = fact.interval.end
        if end is not None and end <= query.at_beat:
            proposed = query.as_point_fact()
            summary = (
                f"proposed '{query.object}' was retired at beat {end} (before beat "
                f"{query.at_beat}) on {query.subject}.{query.predicate}"
            )
            steps = (
                ProofStep(
                    rule=Rule.RETIRED_BEFORE_BEAT,
                    premises=(f"canon: {fact.label()}",),
                    conclusion=(
                        f"'{query.object}' is true only over {fact.interval}; "
                        f"closed at beat {end}"
                    ),
                ),
                ProofStep(
                    rule=Rule.RETIRED_BEFORE_BEAT,
                    premises=(f"queried beat {query.at_beat} ≥ retirement beat {end}",),
                    conclusion=(
                        f"CONTRADICTION: depicting retired '{query.object}' at beat "
                        f"{query.at_beat} (§8.5 forgetting)"
                    ),
                ),
            )
            trace = ProofTrace(
                summary=summary,
                steps=steps,
                contradiction=True,
                cited_fact_ids=(fact.fact_id,) if fact.fact_id else (),
            )
            return Contradiction(
                left=proposed, right=fact, beat=query.at_beat, trace=trace
            )
    return None


__all__ = [
    "Contradiction",
    "check_proposed_fact",
    "detect_canon_contradictions",
]
