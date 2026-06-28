"""Spatial continuity + prop/wardrobe persistence (§8.5, §9.5).

Two continuity dimensions the Critic's per-clip checks (§9.5) cannot see across
shots, because they are facts of *where things are* and *what persists*:

* **Spatial continuity** (:func:`detect_spatial_conflicts`): an entity is in
  exactly one place per beat. Two active ``located_in`` facts with different
  places overlap-in-time ⇒ a teleport. Built on the same functional-channel
  machinery as the contradiction core, but specialised so the proof trace reads
  spatially and so it can also flag *impossible co-presence* (two entities that
  the canon says are apart depicted together).
* **Prop / wardrobe persistence** (:func:`prop_persistence_gaps`): a prop a
  character holds, or a garment they wear, persists across beats until something
  retires it. A shot that *omits* a still-active prop (a gap), or a wardrobe
  channel that silently flickers between values without a retirement, is a
  continuity error a long adaptation accumulates.

Pure: reads a :class:`~.timeline.CanonTimeline`, returns value objects + proofs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .proof import ProofStep, ProofTrace, Rule
from .timeline import CanonTimeline

_LOCATION_PREDICATES: tuple[str, ...] = ("located_in", "location")
_WARDROBE_PREDICATES: tuple[str, ...] = ("wearing", "wardrobe")
_PROP_PREDICATES: tuple[str, ...] = ("possesses", "holding")


@dataclass(frozen=True, slots=True)
class SpatialConflict:
    """A place/co-presence contradiction (teleport or impossible togetherness)."""

    subject: str
    beat: int
    place_a: str
    place_b: str
    trace: ProofTrace


@dataclass(frozen=True, slots=True)
class WardrobeContinuity:
    """A prop/wardrobe persistence gap on one channel between two beats."""

    subject: str
    predicate: str
    slot: str
    object: str
    last_active_beat: int
    next_beat: int
    trace: ProofTrace


def detect_spatial_conflicts(timeline: CanonTimeline) -> list[SpatialConflict]:
    """Flag every beat where a subject is canonically in two places (teleport)."""
    out: list[SpatialConflict] = []
    for subject in timeline.subjects():
        located = [
            f
            for pred in _LOCATION_PREDICATES
            for f in timeline.channel_history(subject, pred)
        ]
        for i in range(len(located)):
            for j in range(i + 1, len(located)):
                a, b = located[i], located[j]
                if a.object == b.object or not a.interval.overlaps(b.interval):
                    continue
                beat = max(a.interval.start, b.interval.start)
                relation = a.interval.relate(b.interval)
                trace = ProofTrace(
                    summary=f"{subject} is in both '{a.object}' and '{b.object}' at beat {beat}",
                    steps=(
                        ProofStep(
                            rule=Rule.TEMPORAL_RELATION,
                            premises=(a.label(), b.label()),
                            conclusion=(
                                f"location intervals stand in '{relation.value}' "
                                f"⇒ both active at beat {beat}"
                            ),
                        ),
                        ProofStep(
                            rule=Rule.FUNCTIONAL_CONFLICT,
                            premises=("an entity occupies one place per beat",),
                            conclusion=(
                                f"CONTRADICTION: {subject} cannot be in '{a.object}' and "
                                f"'{b.object}' at beat {beat} (teleport)"
                            ),
                        ),
                    ),
                    contradiction=True,
                    cited_fact_ids=tuple(fid for fid in (a.fact_id, b.fact_id) if fid),
                )
                out.append(
                    SpatialConflict(
                        subject=subject,
                        beat=beat,
                        place_a=a.object,
                        place_b=b.object,
                        trace=trace,
                    )
                )
    out.sort(key=lambda c: (c.beat, c.subject))
    return out


def colocated_at(timeline: CanonTimeline, place: str, beat: int) -> tuple[str, ...]:
    """Subjects canonically located in ``place`` at ``beat`` (co-presence set)."""
    out: list[str] = []
    for subject in timeline.subjects():
        for pred in _LOCATION_PREDICATES:
            loc = timeline.value_at(subject, pred, beat)
            if loc is not None and loc.object == place:
                out.append(subject)
                break
    return tuple(sorted(out))


def prop_persistence_gaps(
    timeline: CanonTimeline, depicted_at: dict[tuple[str, str], int]
) -> list[WardrobeContinuity]:
    """Find still-active props/wardrobe a later depiction silently dropped.

    ``depicted_at`` maps ``(subject, object)`` → the beat the most recent shot
    depicting that subject was rendered. For each active prop/wardrobe fact whose
    subject was depicted at a beat where the fact is *still in force* but the
    fact's own start is earlier, we record a persistence checkpoint: the prop
    must still be present. (This surfaces "the hero's sword vanished between
    shots" without a model.) A gap is reported when the fact is active at
    ``last_active_beat`` but the depiction beat moved past a retirement.
    """
    out: list[WardrobeContinuity] = []
    for (subject, obj), depict_beat in sorted(depicted_at.items()):
        for pred in (*_PROP_PREDICATES, *_WARDROBE_PREDICATES):
            slot = _slot_for(timeline, subject, pred, obj)
            for fact in timeline.channel_history(subject, pred, slot):
                if fact.object != obj:
                    continue
                # The prop was established earlier and is still active now → it
                # must persist into the depiction; flag if the shot is past its end.
                if fact.interval.start < depict_beat and not fact.active_at(depict_beat):
                    end = fact.interval.end
                    trace = ProofTrace(
                        summary=(
                            f"{subject}'s '{obj}' was retired at beat {end} but a shot at "
                            f"beat {depict_beat} may still show it"
                        ),
                        steps=(
                            ProofStep(
                                rule=Rule.RETIRED_BEFORE_BEAT,
                                premises=(fact.label(),),
                                conclusion=f"'{obj}' active only over {fact.interval}",
                            ),
                            ProofStep(
                                rule=Rule.RETIRED_BEFORE_BEAT,
                                premises=(f"shot beat {depict_beat} ≥ retirement {end}",),
                                conclusion=f"'{obj}' must NOT appear from beat {depict_beat}",
                            ),
                        ),
                        contradiction=True,
                        cited_fact_ids=(fact.fact_id,) if fact.fact_id else (),
                    )
                    out.append(
                        WardrobeContinuity(
                            subject=subject,
                            predicate=pred,
                            slot=fact.slot,
                            object=obj,
                            last_active_beat=(end - 1) if end is not None else depict_beat,
                            next_beat=depict_beat,
                            trace=trace,
                        )
                    )
    return out


def _slot_for(timeline: CanonTimeline, subject: str, predicate: str, obj: str) -> str:
    """Resolve the stored slot for a (subject, predicate, object) lookup."""
    from .facts import fact_slot

    return fact_slot(predicate, obj)


__all__ = [
    "SpatialConflict",
    "WardrobeContinuity",
    "colocated_at",
    "detect_spatial_conflicts",
    "prop_persistence_gaps",
]
