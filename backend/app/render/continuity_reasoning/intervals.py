"""Allen interval algebra over beat-indexed validity intervals (§8.5).

A continuity fact in the canon is true only over a *beat interval*
``[valid_from_beat, valid_to_beat)`` — the §8.5 versioning that makes "timely
forgetting" possible: a fact retired at beat 34 is invisible to forward
generation at beat 50, but preserved for a backward (time-travel) read.

This module is the **pure, network-free temporal core**. It models each fact's
lifetime as a half-open integer interval on the beat axis and computes the
thirteen Allen relations between two intervals. Everything else in the
continuity-reasoning engine (contradiction detection, propagation, multi-hop
inference) reasons in terms of these relations, so the temporal logic is one
small exhaustively-tested unit rather than scattered ``<``/``>`` comparisons.

Why half-open ``[from, to)``: a fact asserted at beat 12 and retired at beat 34
is active for beats 12..33 and *not* at 34 (the beat it was retired). An
open-ended (still-true) fact has ``end = None``, treated as ``+∞``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

#: Sentinel for an open-ended (never-retired) fact: ``+∞`` on the beat axis.
OPEN_END = None

_POS_INF = float("inf")


class Allen(StrEnum):
    """The thirteen Allen interval relations (plus their inverses).

    Read ``X r Y`` as "interval X is *r* relative to interval Y". The six
    asymmetric relations have inverses (``BEFORE``/``AFTER`` etc.); ``EQUALS`` is
    its own inverse.
    """

    BEFORE = "before"  # X ends strictly before Y starts (a gap between them)
    AFTER = "after"  # inverse of BEFORE
    MEETS = "meets"  # X ends exactly where Y starts (no gap, no overlap)
    MET_BY = "met_by"  # inverse of MEETS
    OVERLAPS = "overlaps"  # X starts first, they share a middle, Y ends later
    OVERLAPPED_BY = "overlapped_by"  # inverse of OVERLAPS
    STARTS = "starts"  # same start, X ends first
    STARTED_BY = "started_by"  # inverse of STARTS
    DURING = "during"  # X strictly inside Y
    CONTAINS = "contains"  # inverse of DURING
    FINISHES = "finishes"  # same end, X starts later
    FINISHED_BY = "finished_by"  # inverse of FINISHES
    EQUALS = "equals"  # identical intervals


#: The set of relations under which two intervals share at least one beat.
OVERLAP_RELATIONS: frozenset[Allen] = frozenset(
    {
        Allen.OVERLAPS,
        Allen.OVERLAPPED_BY,
        Allen.STARTS,
        Allen.STARTED_BY,
        Allen.DURING,
        Allen.CONTAINS,
        Allen.FINISHES,
        Allen.FINISHED_BY,
        Allen.EQUALS,
    }
)

_INVERSES: dict[Allen, Allen] = {
    Allen.BEFORE: Allen.AFTER,
    Allen.AFTER: Allen.BEFORE,
    Allen.MEETS: Allen.MET_BY,
    Allen.MET_BY: Allen.MEETS,
    Allen.OVERLAPS: Allen.OVERLAPPED_BY,
    Allen.OVERLAPPED_BY: Allen.OVERLAPS,
    Allen.STARTS: Allen.STARTED_BY,
    Allen.STARTED_BY: Allen.STARTS,
    Allen.DURING: Allen.CONTAINS,
    Allen.CONTAINS: Allen.DURING,
    Allen.FINISHES: Allen.FINISHED_BY,
    Allen.FINISHED_BY: Allen.FINISHES,
    Allen.EQUALS: Allen.EQUALS,
}


def inverse(relation: Allen) -> Allen:
    """Return the converse relation: if ``X r Y`` then ``Y inverse(r) X``."""
    return _INVERSES[relation]


@dataclass(frozen=True, slots=True)
class BeatInterval:
    """A half-open beat interval ``[start, end)`` (``end=None`` ⇒ open-ended).

    ``start`` is the (inclusive) beat the fact becomes true; ``end`` is the
    (exclusive) beat it is no longer true — i.e. the beat it was retired. A fact
    asserted at beat 12 and never retired is ``BeatInterval(12, None)``.
    """

    start: int
    end: int | None = OPEN_END

    def __post_init__(self) -> None:
        if self.end is not None and self.end < self.start:
            raise ValueError(f"interval end ({self.end}) precedes start ({self.start})")

    @property
    def is_open(self) -> bool:
        """``True`` if the fact is still in force (never retired)."""
        return self.end is None

    @property
    def _hi(self) -> float:
        """The end as a float, with ``+∞`` for open-ended intervals."""
        return _POS_INF if self.end is None else float(self.end)

    def contains_beat(self, beat: int) -> bool:
        """``True`` iff the fact is *active at* ``beat`` (half-open membership)."""
        return self.start <= beat < self._hi

    def relate(self, other: BeatInterval) -> Allen:
        """Return the single Allen relation of ``self`` relative to ``other``.

        Exactly one of the thirteen relations holds between any two intervals.
        Open-ended intervals compare with ``+∞`` as their upper bound, so two
        still-open facts that start together are :attr:`Allen.EQUALS`, and an
        open fact that starts later than another open fact :attr:`Allen.FINISHES`
        it (both run to ``+∞``).
        """
        a_lo, a_hi = self.start, self._hi
        b_lo, b_hi = other.start, other._hi

        if a_lo == b_lo and a_hi == b_hi:
            return Allen.EQUALS
        if a_hi < b_lo:
            return Allen.BEFORE
        if a_hi == b_lo:
            return Allen.MEETS
        if a_lo > b_hi:
            return Allen.AFTER
        if a_lo == b_hi:
            return Allen.MET_BY
        # The intervals overlap in their interior; classify by endpoints.
        if a_lo == b_lo:
            return Allen.STARTS if a_hi < b_hi else Allen.STARTED_BY
        if a_hi == b_hi:
            return Allen.FINISHES if a_lo > b_lo else Allen.FINISHED_BY
        if a_lo < b_lo:
            return Allen.OVERLAPS if a_hi < b_hi else Allen.CONTAINS
        # a_lo > b_lo
        return Allen.DURING if a_hi < b_hi else Allen.OVERLAPPED_BY

    def overlaps(self, other: BeatInterval) -> bool:
        """``True`` iff the two intervals share at least one beat."""
        return self.relate(other) in OVERLAP_RELATIONS

    def __str__(self) -> str:
        hi = "∞" if self.end is None else str(self.end)
        return f"[{self.start}, {hi})"


__all__ = [
    "OPEN_END",
    "OVERLAP_RELATIONS",
    "Allen",
    "BeatInterval",
    "inverse",
]
