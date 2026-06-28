"""The versioned-fact model the continuity reasoners operate on (§8.1, §8.5).

The canon's continuity-state node (§8.1) is a *versioned fact*: a
``(subject, predicate, object)`` triple true only over a beat interval. The
memory layer ships these as :class:`app.memory.interfaces.StateSlice` (active
facts only) or, for time-travel reads, with explicit ``valid_to_beat``.

:class:`Fact` is the pure, hashable in-engine representation of one such triple
plus its :class:`~.intervals.BeatInterval` lifetime and provenance. It is
deliberately decoupled from the Pydantic ``StateSlice`` so the reasoners never
import the memory/agents layer — :func:`fact_from_state_slice` is the one
adapter, and the engine itself is testable with hand-built :class:`Fact`s.

Two orthogonal axes a fact carries beyond the triple:

* **Functional predicates** (:data:`FUNCTIONAL_PREDICATES`): predicates that
  admit at most one value per subject at any beat (a character is in exactly one
  *location*; holds exactly one *possesses:<slot>*). Two overlapping facts on a
  functional predicate with different objects are a contradiction — this is the
  core temporal-contradiction rule (§8.5).
* **Epistemic visibility** (:data:`Visibility`): whether the *reader* currently
  knows the fact, distinct from whether it is canonically true. A fact can be
  canon-true yet reader-unknown (dramatic irony) — the §10 Critic must not
  "spoil" by depicting reader-unknown facts, and Continuity tracks the gap.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from .intervals import BeatInterval


@runtime_checkable
class StateLike(Protocol):
    """The duck-typed shape of a memory-layer ``StateSlice`` the adapter reads.

    Declared as a Protocol so :func:`fact_from_state_slice` types cleanly without
    importing :mod:`app.memory.interfaces` (the reasoners stay dependency-free);
    the real ``StateSlice`` and any test double with these attributes fit.
    """

    state_id: str
    subject_entity_key: str
    predicate: str
    object_value: str
    valid_from_beat: int
    valid_to_beat: int | None

#: Predicates that are *functional*: a subject may have at most one object value
#: at any single beat. Two active facts that disagree on the object are a
#: contradiction. ``possesses`` is functional *per slot* (see :func:`fact_slot`).
FUNCTIONAL_PREDICATES: frozenset[str] = frozenset(
    {
        "located_in",
        "location",
        "is",  # a copular state: "is unarmed" / "is alive" (one value at a time)
        "state",
        "wearing",  # wardrobe slot (qualified by slot, see fact_slot)
        "holding",
        "possesses",  # qualified by slot
        "alive_status",
        "emotional_state",
    }
)

#: Predicates whose object names another entity the fact *depends on* — used by
#: propagation: if the depended-on entity's state changes, dependents may need
#: review (e.g. a prop a character ``possesses`` is destroyed).
ENTITY_VALUED_PREDICATES: frozenset[str] = frozenset(
    {"possesses", "holding", "located_in", "location", "accompanied_by", "wearing"}
)


class Visibility(StrEnum):
    """What the *reader* knows about a fact, vs. what is canonically true (§10).

    The canon graph stores ground truth; the epistemic layer tracks the reader's
    knowledge frontier so the system never depicts a twist the reader has not yet
    reached (a spoiler) and can reason about dramatic irony.
    """

    #: The reader has been shown / told this fact (it is on-page knowledge).
    KNOWN = "known"
    #: Canon-true but the reader has not yet learned it (a pending reveal).
    HIDDEN = "hidden"
    #: The reader believes something the canon contradicts (a false belief held
    #: until a reveal corrects it — dramatic irony from the reader's side).
    MISTAKEN = "mistaken"


@dataclass(frozen=True, slots=True)
class Fact:
    """One versioned continuity fact: a triple with a beat-interval lifetime.

    Frozen + hashable so facts can live in sets and be keyed in dicts during
    inference. ``slot`` disambiguates functional predicates that have several
    independent channels (``possesses:weapon`` vs ``possesses:cloak``).
    """

    subject: str
    predicate: str
    object: str
    interval: BeatInterval
    fact_id: str = ""
    #: Sub-channel of a functional predicate (e.g. wardrobe slot); "" = default.
    slot: str = ""
    #: Reader-knowledge state of this fact (default: shown when it becomes true).
    visibility: Visibility = Visibility.KNOWN
    #: The beat the reader learns a HIDDEN fact (the reveal); ``None`` = unset.
    revealed_at_beat: int | None = None
    #: Free-form source-span note for human-readable proof traces.
    source: str = ""

    @property
    def channel(self) -> tuple[str, str, str]:
        """The (subject, predicate, slot) channel this fact competes within.

        Functional uniqueness is per-channel: two facts on the same channel that
        overlap in time and disagree on ``object`` contradict.
        """
        return (self.subject, self.predicate, self.slot)

    @property
    def is_functional(self) -> bool:
        """``True`` if at most one object value may hold per beat on this channel."""
        return self.predicate in FUNCTIONAL_PREDICATES

    @property
    def is_entity_valued(self) -> bool:
        """``True`` if ``object`` names another entity this fact depends on."""
        return self.predicate in ENTITY_VALUED_PREDICATES

    def active_at(self, beat: int) -> bool:
        """``True`` iff this fact is in force at ``beat`` (half-open membership)."""
        return self.interval.contains_beat(beat)

    def known_to_reader_at(self, beat: int) -> bool:
        """``True`` iff the reader knows this fact by ``beat`` (epistemic, §10).

        A ``KNOWN`` fact is known once it is active; a ``HIDDEN`` fact only
        becomes reader-known at ``revealed_at_beat`` (and only if also active).
        A ``MISTAKEN`` belief is "known" in the sense the reader holds it.
        """
        if not self.active_at(beat):
            # A reveal can land before/at the fact's own start; honour it.
            if self.visibility is Visibility.HIDDEN and self.revealed_at_beat is not None:
                return beat >= self.revealed_at_beat and beat >= self.interval.start
            return False
        if self.visibility is Visibility.HIDDEN:
            return self.revealed_at_beat is not None and beat >= self.revealed_at_beat
        return True

    def label(self) -> str:
        """A compact human-readable rendering used in proof traces."""
        slot = f"[{self.slot}]" if self.slot else ""
        ident = f"{self.fact_id}: " if self.fact_id else ""
        return f"{ident}{self.subject} {self.predicate}{slot} {self.object} {self.interval}"

    def __str__(self) -> str:
        return self.label()


@dataclass(frozen=True, slots=True)
class FactQuery:
    """A proposed depiction expressed as a fact to test against the canon.

    The Cinematographer's shot implies the entity is in some state at a beat
    (drawing a sword ⇒ ``possesses[weapon] sword``). Expressing the depiction as
    a :class:`FactQuery` lets the reasoner check it against the canon with the
    same temporal machinery used for canon-vs-canon contradictions.
    """

    subject: str
    predicate: str
    object: str
    at_beat: int
    slot: str = ""

    @property
    def channel(self) -> tuple[str, str, str]:
        return (self.subject, self.predicate, self.slot)

    def as_point_fact(self, *, fact_id: str = "proposed") -> Fact:
        """The query as a degenerate single-beat fact for relation reasoning."""
        return Fact(
            subject=self.subject,
            predicate=self.predicate,
            object=self.object,
            interval=BeatInterval(self.at_beat, self.at_beat + 1),
            fact_id=fact_id,
            slot=self.slot,
            source="proposed shot",
        )


def fact_slot(predicate: str, object_value: str) -> str:
    """Best-effort slot for a functional predicate that has sub-channels.

    ``possesses``/``holding`` of a typed prop competes per-type (a hero may hold a
    sword *and* a torch); we key the slot by a coarse object category so the same
    weapon being drawn/sheathed competes but a torch does not collide with it.
    Pure and conservative: unknown objects fall back to the empty slot.
    """
    obj = object_value.lower()
    if predicate not in {"possesses", "holding", "wearing"}:
        return ""
    for keyword, slot in _SLOT_KEYWORDS:
        if keyword in obj:
            return slot
    return ""


_SLOT_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("sword", "weapon"),
    ("dagger", "weapon"),
    ("blade", "weapon"),
    ("bow", "weapon"),
    ("gun", "weapon"),
    ("knife", "weapon"),
    ("staff", "weapon"),
    ("torch", "light"),
    ("lantern", "light"),
    ("candle", "light"),
    ("cloak", "outerwear"),
    ("coat", "outerwear"),
    ("cape", "outerwear"),
    ("crown", "headwear"),
    ("hat", "headwear"),
    ("helm", "headwear"),
    ("ring", "jewellery"),
    ("amulet", "jewellery"),
    ("necklace", "jewellery"),
)


def fact_from_state_slice(
    state: StateLike,
    *,
    visibility: Visibility = Visibility.KNOWN,
) -> Fact:
    """Adapt a memory-layer ``StateSlice`` (duck-typed) into a :class:`Fact`.

    Reads the attributes the engine needs (``state_id``,
    ``subject_entity_key``, ``predicate``, ``object_value``, ``valid_from_beat``,
    ``valid_to_beat``) without importing the memory package, so the reasoners
    stay free of that dependency. Anything matching :class:`StateLike` fits.
    """
    valid_to = state.valid_to_beat
    predicate = str(state.predicate)
    object_value = str(state.object_value)
    return Fact(
        subject=str(state.subject_entity_key),
        predicate=predicate,
        object=object_value,
        interval=BeatInterval(int(state.valid_from_beat), valid_to),
        fact_id=str(state.state_id),
        slot=fact_slot(predicate, object_value),
        visibility=visibility,
    )


__all__ = [
    "ENTITY_VALUED_PREDICATES",
    "FUNCTIONAL_PREDICATES",
    "Fact",
    "FactQuery",
    "StateLike",
    "Visibility",
    "fact_from_state_slice",
    "fact_slot",
]
