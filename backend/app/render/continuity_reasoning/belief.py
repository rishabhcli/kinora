"""Reader belief revision — the unreliable-narrator / misdirection layer (§10).

The epistemic layer (``epistemic.py``) tracks *what the reader knows* vs. canon
truth. This module models the harder case: what the reader **wrongly believes**.
A mystery, a twist, or an unreliable narrator works because the reader holds a
*false* belief over a stretch of the story, which a later reveal corrects. A
faithful page-synced adaptation must render *that belief*, not the ground truth:
before the reveal, the shot should show what the reader thinks is true; at the
reveal it flips. Depicting the canonical truth early is the spoiler the §10
Critic's "don't be charitable" discipline forbids.

A :class:`ReaderBelief` is a fact the reader holds true over a belief interval
(possibly contradicting the canon). The :class:`BeliefState` composes the canon
timeline with the reader's beliefs and answers, per beat:

* what the reader currently believes on a channel (which may differ from canon),
* whether a depiction matches the reader's belief (render-target check),
* the **revision** that fires at a reveal — the belief the reader must drop and
  the canon fact they adopt — with a proof trace.

Pure: reads a :class:`~.timeline.CanonTimeline` + a list of beliefs, returns
value objects. No I/O, no model.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .facts import Fact, FactQuery, Visibility
from .intervals import BeatInterval
from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline


@dataclass(frozen=True, slots=True)
class ReaderBelief:
    """A fact the reader holds true over ``interval`` — possibly contrary to canon.

    ``corrected_at_beat`` is the reveal that ends the belief (``None`` if the
    belief was always correct / never corrected). ``mistaken`` marks a belief the
    canon contradicts (dramatic irony / misdirection); a non-mistaken belief
    simply mirrors canon truth the reader happens to hold.
    """

    subject: str
    predicate: str
    object: str
    interval: BeatInterval
    slot: str = ""
    mistaken: bool = False
    corrected_at_beat: int | None = None
    source: str = ""

    @property
    def channel(self) -> tuple[str, str, str]:
        return (self.subject, self.predicate, self.slot)

    def held_at(self, beat: int) -> bool:
        """``True`` iff the reader still holds this belief at ``beat``.

        A belief is held over its interval, but a reveal at ``corrected_at_beat``
        ends it early (the reader revises at the reveal).
        """
        if self.corrected_at_beat is not None and beat >= self.corrected_at_beat:
            return False
        return self.interval.contains_beat(beat)

    def as_fact(self) -> Fact:
        return Fact(
            subject=self.subject,
            predicate=self.predicate,
            object=self.object,
            interval=self.interval,
            fact_id=f"belief_{self.subject}_{self.predicate}_{self.object}",
            slot=self.slot,
            visibility=Visibility.MISTAKEN if self.mistaken else Visibility.KNOWN,
            source=self.source or "reader belief",
        )


@dataclass(frozen=True, slots=True)
class BeliefRevision:
    """The revision fired at a reveal: drop a false belief, adopt the canon fact."""

    beat: int
    dropped: ReaderBelief
    adopted: Fact | None
    trace: ProofTrace


@dataclass(frozen=True, slots=True)
class BeliefState:
    """The reader's belief world layered over the canon (per-beat queries)."""

    timeline: CanonTimeline
    beliefs: tuple[ReaderBelief, ...] = field(default_factory=tuple)

    @classmethod
    def build(
        cls, timeline: CanonTimeline, beliefs: list[ReaderBelief]
    ) -> BeliefState:
        return cls(timeline=timeline, beliefs=tuple(beliefs))

    def believed_value(self, subject: str, predicate: str, beat: int, slot: str = "") -> str | None:
        """What the reader believes on a channel at ``beat`` (belief wins over canon).

        If the reader holds a belief on the channel, that is the render target;
        otherwise the canon's active value (the reader believes the canon by
        default). ``None`` if neither has a value.
        """
        held = [
            b
            for b in self.beliefs
            if b.channel == (subject, predicate, slot) and b.held_at(beat)
        ]
        if held:
            return min(held, key=lambda b: b.interval.start).object
        canon = self.timeline.value_at(subject, predicate, beat, slot)
        return canon.object if canon is not None else None

    def matches_reader_belief(self, query: FactQuery) -> bool:
        """``True`` iff depicting ``query`` matches what the reader believes now.

        The render-target check: before a reveal a shot should depict the reader's
        (possibly false) belief, so a depiction that matches it is *correct* even
        when it contradicts the canon. A depiction matching neither belief nor a
        held value is left to the contradiction reasoner.
        """
        believed = self.believed_value(query.subject, query.predicate, query.at_beat, query.slot)
        return believed is not None and believed == query.object

    def dramatic_irony_at(self, beat: int) -> list[tuple[ReaderBelief, Fact]]:
        """Pairs of (false belief held, contradicting canon fact) active at ``beat``."""
        out: list[tuple[ReaderBelief, Fact]] = []
        for belief in self.beliefs:
            if not (belief.mistaken and belief.held_at(beat)):
                continue
            canon = self.timeline.value_at(
                belief.subject, belief.predicate, beat, belief.slot
            )
            if canon is not None and canon.object != belief.object:
                out.append((belief, canon))
        return out

    def revisions(self) -> list[BeliefRevision]:
        """Every belief revision the reader undergoes (one per corrected belief).

        At ``corrected_at_beat`` the reader drops the false belief and adopts the
        canon fact then active — the moment the misdirection resolves. Ordered by
        reveal beat for a stable timeline.
        """
        out: list[BeliefRevision] = []
        for belief in self.beliefs:
            beat = belief.corrected_at_beat
            if beat is None or not belief.mistaken:
                continue
            adopted = self.timeline.value_at(belief.subject, belief.predicate, beat, belief.slot)
            steps = (
                ProofStep(
                    rule=Rule.READER_MISBELIEF,
                    premises=(
                        f"reader believed {belief.subject}.{belief.predicate} = "
                        f"'{belief.object}' over {belief.interval}",
                        f"canon: {adopted.label() if adopted else 'no active fact'}",
                    ),
                    conclusion=f"the belief is false; a reveal is scheduled at beat {beat}",
                ),
                ProofStep(
                    rule=Rule.READER_MISBELIEF,
                    premises=(f"reveal beat {beat} reached",),
                    conclusion=(
                        f"REVISION: drop '{belief.object}', adopt "
                        f"'{adopted.object if adopted else '∅'}' from beat {beat}"
                    ),
                ),
            )
            out.append(
                BeliefRevision(
                    beat=beat,
                    dropped=belief,
                    adopted=adopted,
                    trace=ProofTrace(
                        summary=(
                            f"reader revises {belief.subject}.{belief.predicate} at beat {beat}"
                        ),
                        steps=steps,
                        contradiction=False,
                    ),
                )
            )
        out.sort(key=lambda r: r.beat)
        return out


__all__ = ["BeliefRevision", "BeliefState", "ReaderBelief"]
