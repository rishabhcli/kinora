"""The CanonTimeline — an indexed, queryable temporal model of the canon (§8.5).

Where the memory layer stores facts row-by-row, the reasoning engine needs them
*organised on the beat axis* so it can ask, in one place and purely:

* which facts are active at a beat (the §8.4 active-retrieval set),
* what value a functional channel holds at a beat,
* the full version history of one channel (for time-travel reads, §8.5),
* every fact about / mentioning an entity (for propagation + multi-hop).

:class:`CanonTimeline` is an immutable snapshot built from a list of
:class:`~.facts.Fact`. It owns no I/O — :meth:`from_state_slices` adapts a
memory ``canon.query`` result, and the agents layer feeds it. All queries are
pure and indexed, so a 300-page book's continuity reasoning stays cheap and the
behaviour is exhaustively unit-testable.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .facts import Fact, StateLike, Visibility, fact_from_state_slice
from .intervals import BeatInterval


@dataclass(frozen=True, slots=True)
class CanonTimeline:
    """An indexed snapshot of versioned facts, queryable by beat / entity / channel."""

    facts: tuple[Fact, ...]
    _by_channel: dict[tuple[str, str, str], tuple[Fact, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _by_subject: dict[str, tuple[Fact, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _by_object: dict[str, tuple[Fact, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    # --- construction ----------------------------------------------------- #

    @classmethod
    def build(cls, facts: Iterable[Fact]) -> CanonTimeline:
        """Build an indexed timeline from facts (the only constructor to use)."""
        ordered = tuple(
            sorted(facts, key=lambda f: (f.interval.start, f.subject, f.predicate, f.fact_id))
        )
        by_channel: dict[tuple[str, str, str], list[Fact]] = defaultdict(list)
        by_subject: dict[str, list[Fact]] = defaultdict(list)
        by_object: dict[str, list[Fact]] = defaultdict(list)
        for fact in ordered:
            by_channel[fact.channel].append(fact)
            by_subject[fact.subject].append(fact)
            by_object[fact.object].append(fact)
        return cls(
            facts=ordered,
            _by_channel={k: tuple(v) for k, v in by_channel.items()},
            _by_subject={k: tuple(v) for k, v in by_subject.items()},
            _by_object={k: tuple(v) for k, v in by_object.items()},
        )

    @classmethod
    def from_state_slices(
        cls,
        states: Sequence[StateLike],
        *,
        hidden_state_ids: Iterable[str] = (),
        revealed_at: dict[str, int] | None = None,
    ) -> CanonTimeline:
        """Build from memory-layer ``StateSlice`` rows (the §8.4 active set).

        ``hidden_state_ids`` marks facts that are canon-true but not yet known to
        the reader (epistemic layer); ``revealed_at`` gives the beat each becomes
        reader-known. Slices not listed default to :attr:`Visibility.KNOWN`.
        """
        hidden = set(hidden_state_ids)
        reveals = revealed_at or {}
        out: list[Fact] = []
        for state in states:
            sid = str(state.state_id)
            visibility = Visibility.HIDDEN if sid in hidden else Visibility.KNOWN
            fact = fact_from_state_slice(state, visibility=visibility)
            if sid in reveals:
                fact = _with_reveal(fact, reveals[sid])
            out.append(fact)
        return cls.build(out)

    def with_facts(self, extra: Iterable[Fact]) -> CanonTimeline:
        """Return a new timeline with ``extra`` facts merged in (immutable)."""
        return CanonTimeline.build([*self.facts, *extra])

    # --- queries ---------------------------------------------------------- #

    def active_at(self, beat: int) -> tuple[Fact, ...]:
        """Facts in force at ``beat`` — the §8.4 active-retrieval set."""
        return tuple(f for f in self.facts if f.active_at(beat))

    def channel_history(self, subject: str, predicate: str, slot: str = "") -> tuple[Fact, ...]:
        """The full version history of one functional channel (time-ordered)."""
        return self._by_channel.get((subject, predicate, slot), ())

    def value_at(
        self, subject: str, predicate: str, beat: int, slot: str = ""
    ) -> Fact | None:
        """The single active fact on a functional channel at ``beat`` (or ``None``).

        If the channel is over-determined at ``beat`` (a real contradiction in
        the canon), the *earliest-started* active fact is returned — the
        contradiction detector is what flags the clash; this getter is total.
        """
        active = [f for f in self.channel_history(subject, predicate, slot) if f.active_at(beat)]
        if not active:
            return None
        return min(active, key=lambda f: (f.interval.start, f.fact_id))

    def facts_about(self, entity: str) -> tuple[Fact, ...]:
        """Every fact whose *subject* is ``entity`` (time-ordered)."""
        return self._by_subject.get(entity, ())

    def facts_mentioning(self, entity: str) -> tuple[Fact, ...]:
        """Facts whose subject *or* object is ``entity`` (for propagation)."""
        subj = self._by_subject.get(entity, ())
        obj = self._by_object.get(entity, ())
        seen: set[int] = set()
        out: list[Fact] = []
        for fact in (*subj, *obj):
            key = id(fact)
            if key not in seen:
                seen.add(key)
                out.append(fact)
        return tuple(out)

    def subjects(self) -> tuple[str, ...]:
        """All distinct fact subjects (entities with state), sorted."""
        return tuple(sorted(self._by_subject))

    def functional_channels(self) -> tuple[tuple[str, str, str], ...]:
        """All functional channels present (the contradiction detector iterates these)."""
        return tuple(sorted(ch for ch in self._by_channel if _is_functional_channel(ch)))

    def span(self) -> BeatInterval:
        """The smallest interval covering every fact (open if any fact is open)."""
        if not self.facts:
            return BeatInterval(0, 0)
        lo = min(f.interval.start for f in self.facts)
        if any(f.interval.is_open for f in self.facts):
            return BeatInterval(lo, None)
        hi = max(f.interval.end for f in self.facts if f.interval.end is not None)
        return BeatInterval(lo, hi)


def _is_functional_channel(channel: tuple[str, str, str]) -> bool:
    from .facts import FUNCTIONAL_PREDICATES

    return channel[1] in FUNCTIONAL_PREDICATES


def _with_reveal(fact: Fact, beat: int) -> Fact:
    """A copy of ``fact`` marked hidden-until-``beat`` (epistemic reveal)."""
    return Fact(
        subject=fact.subject,
        predicate=fact.predicate,
        object=fact.object,
        interval=fact.interval,
        fact_id=fact.fact_id,
        slot=fact.slot,
        visibility=Visibility.HIDDEN,
        revealed_at_beat=beat,
        source=fact.source,
    )


__all__ = ["CanonTimeline"]
