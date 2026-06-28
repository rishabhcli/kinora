"""Multi-hop transitive inference over the canon (§8.5, §10).

A continuity engine that only reads stored facts misses the *implied* ones. If
the hero ``possesses`` the lantern and the hero ``located_in`` the cellar at
beat 40, then the lantern is in the cellar at beat 40 — a fact no one asserted
but which a shot can contradict. Multi-hop inference derives these transitively
so the contradiction detector can reason about depictions that are wrong only by
composition.

Two transitive families are modelled, both **pure** and beat-scoped:

* **Carried location** (:func:`transitive_location`): ``A located_in L`` ∧
  ``A possesses/holding P`` ⟹ ``P located_in L`` (the prop is wherever its
  holder is). One hop, but it composes with accompaniment.
* **Accompaniment closure** (:func:`multi_hop_closure`): ``A accompanied_by B`` ∧
  ``A located_in L`` ⟹ ``B located_in L``, chained transitively (a travelling
  party shares a location), with a per-derivation depth cap to stay terminating.

Each derived :class:`InferredFact` carries the proof chain that produced it, so a
contradiction found against an *inferred* fact still explains itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from .facts import Fact
from .intervals import BeatInterval
from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline

#: Predicates that place a *prop* at its holder's location when carried.
_CARRY_PREDICATES: frozenset[str] = frozenset({"possesses", "holding"})
#: The location predicates (normalised to ``located_in`` in derived facts).
_LOCATION_PREDICATES: frozenset[str] = frozenset({"located_in", "location"})
#: Default cap on accompaniment chaining (a party of ~8 is generous).
_DEFAULT_MAX_HOPS = 8


@dataclass(frozen=True, slots=True)
class InferredFact:
    """A fact derived (not stored), carrying the chain that produced it."""

    fact: Fact
    trace: ProofTrace
    hops: int


def transitive_location(timeline: CanonTimeline, beat: int) -> list[InferredFact]:
    """Place every carried prop at its holder's location at ``beat`` (one hop).

    For each subject located somewhere at ``beat`` who also possesses/holds a
    prop, derive ``prop located_in <holder's location>`` with a two-premise
    proof. Skips a prop that already has its *own* explicit location fact (an
    explicit fact wins over the inferred one).
    """
    out: list[InferredFact] = []
    for subject in timeline.subjects():
        loc = _location_of(timeline, subject, beat)
        if loc is None:
            continue
        for fact in timeline.facts_about(subject):
            if fact.predicate not in _CARRY_PREDICATES or not fact.active_at(beat):
                continue
            prop = fact.object
            if _location_of(timeline, prop, beat) is not None:
                continue  # explicit location for the prop exists; don't override
            derived = Fact(
                subject=prop,
                predicate="located_in",
                object=loc.object,
                interval=BeatInterval(beat, beat + 1),
                fact_id=f"inf_{prop}_{loc.object}_{beat}",
                source="inferred (carried)",
            )
            trace = ProofTrace(
                summary=f"{prop} is in {loc.object} at beat {beat} (carried by {subject})",
                steps=(
                    ProofStep(
                        rule=Rule.TRANSITIVE,
                        premises=(fact.label(), loc.label()),
                        conclusion=f"{subject} carries {prop} and is in {loc.object}",
                    ),
                    ProofStep(
                        rule=Rule.TRANSITIVE,
                        premises=("a carried prop shares its holder's location",),
                        conclusion=f"{prop} located_in {loc.object} @ beat {beat}",
                    ),
                ),
                contradiction=False,
                cited_fact_ids=tuple(fid for fid in (fact.fact_id, loc.fact_id) if fid),
            )
            out.append(InferredFact(fact=derived, trace=trace, hops=1))
    out.sort(key=lambda i: (i.fact.subject, i.fact.object))
    return out


def multi_hop_closure(
    timeline: CanonTimeline, beat: int, *, max_hops: int = _DEFAULT_MAX_HOPS
) -> list[InferredFact]:
    """Close accompaniment + carried-location at ``beat`` (terminating, capped).

    Computes the location of every entity reachable from a located entity via
    ``accompanied_by`` edges (a travelling party), then places carried props at
    those derived locations. ``max_hops`` bounds the accompaniment chain so the
    closure always terminates even on a cyclic ``accompanied_by`` graph.
    """
    # Seed: entities with an explicit location at the beat.
    location: dict[str, Fact] = {}
    derived_traces: dict[str, ProofTrace] = {}
    derived_hops: dict[str, int] = {}
    for subject in timeline.subjects():
        loc = _location_of(timeline, subject, beat)
        if loc is not None:
            location[subject] = loc
            derived_hops[subject] = 0

    # Breadth-first over accompaniment edges, capped by hop count.
    frontier = list(location)
    hop = 0
    inferred: list[InferredFact] = []
    while frontier and hop < max_hops:
        hop += 1
        next_frontier: list[str] = []
        for who in frontier:
            anchor_loc = location.get(who)
            if anchor_loc is None:
                continue
            for fact in timeline.facts_about(who):
                if fact.predicate != "accompanied_by" or not fact.active_at(beat):
                    continue
                companion = fact.object
                if companion in location:
                    continue  # already placed (explicit or earlier hop)
                companion_loc = Fact(
                    subject=companion,
                    predicate="located_in",
                    object=anchor_loc.object,
                    interval=BeatInterval(beat, beat + 1),
                    fact_id=f"inf_{companion}_{anchor_loc.object}_{beat}",
                    source=f"inferred (accompanies {who})",
                )
                location[companion] = companion_loc
                derived_hops[companion] = hop
                trace = ProofTrace(
                    summary=f"{companion} is in {anchor_loc.object} at beat {beat} (with {who})",
                    steps=(
                        ProofStep(
                            rule=Rule.TRANSITIVE,
                            premises=(fact.label(), anchor_loc.label()),
                            conclusion=(
                                f"{who} is in {anchor_loc.object} and travels "
                                f"with {companion}"
                            ),
                        ),
                        ProofStep(
                            rule=Rule.TRANSITIVE,
                            premises=(f"hop {hop} ≤ cap {max_hops}",),
                            conclusion=f"{companion} located_in {anchor_loc.object} @ beat {beat}",
                        ),
                    ),
                    contradiction=False,
                    cited_fact_ids=tuple(fid for fid in (fact.fact_id, anchor_loc.fact_id) if fid),
                )
                derived_traces[companion] = trace
                inferred.append(InferredFact(fact=companion_loc, trace=trace, hops=hop))
                next_frontier.append(companion)
        frontier = next_frontier

    # Carried-location over the *closed* location map.
    closed = timeline.with_facts(i.fact for i in inferred)
    inferred.extend(transitive_location(closed, beat))
    inferred.sort(key=lambda i: (i.hops, i.fact.subject, i.fact.object))
    return _dedup(inferred)


def _location_of(timeline: CanonTimeline, entity: str, beat: int) -> Fact | None:
    for predicate in ("located_in", "location"):
        loc = timeline.value_at(entity, predicate, beat)
        if loc is not None:
            return loc
    return None


def _dedup(items: list[InferredFact]) -> list[InferredFact]:
    seen: set[tuple[str, str, str]] = set()
    out: list[InferredFact] = []
    for item in items:
        key = (item.fact.subject, item.fact.predicate, item.fact.object)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


__all__ = ["InferredFact", "multi_hop_closure", "transitive_location"]
