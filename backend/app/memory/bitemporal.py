"""Bitemporal value objects + interval algebra (kinora.md §8.1, §8.5).

The existing canon (``entities`` / ``continuity_states``) is **uni-temporal**: a fact
carries a *valid-time* interval measured in **beat ordinals** (the story timeline). To
answer "what did the canon believe *as of* a past write" — and to make director edits and
Critic conflict-resolutions auditable — we add a second, orthogonal **transaction-time**
axis measured in **wall-clock UTC**.

A fact is therefore pinned by two half-open intervals:

* **valid** ``[valid_from_beat, valid_to_beat)`` — *when in the book* the fact holds. §8.5
  forgetting closes ``valid_to_beat``; the row survives for backward reads.
* **tx** ``[tx_from, tx_to)`` — *when the system believed it*. A correction closes ``tx_to``
  and inserts a successor, so every past belief is reconstructable.

This module is **pure** (no DB, no network): just the value objects and the interval
algebra (containment, overlap, the Allen interval relations). It is the offline-testable
core every bitemporal service builds on, in the spirit of the project's "unit suite runs
with no infra" rule.

Intervals are **half-open** ``[lo, hi)`` so adjacent intervals tile without overlap — the
single most important invariant for time-travel: at the boundary beat/instant, exactly one
version is active. ``hi=None`` means *open* (still current); it compares as +∞.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from datetime import UTC, datetime

# Resolving "as of the latest version" = resolving at a beat beyond any real one. Shared
# with ``canon_service._LATEST_BEAT`` so the two read paths agree on "now".
LATEST_BEAT: int = 2**31 - 1


def utcnow() -> datetime:
    """Timezone-aware UTC now — the single clock source for transaction-time."""
    return datetime.now(UTC)


def _as_aware(value: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC (naive inputs are assumed UTC)."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class Allen(enum.StrEnum):
    """The thirteen Allen interval relations (the algebra of "before/overlaps/during").

    Used by the bitemporal reasoner to classify how two valid-intervals relate — e.g. two
    facts that *overlap* on the same (subject, predicate) are candidate contradictions
    (§9.5 timeline check), whereas *meets* (one ends exactly where the next begins) is the
    clean supersession a correction produces.
    """

    BEFORE = "before"
    MEETS = "meets"
    OVERLAPS = "overlaps"
    STARTS = "starts"
    DURING = "during"
    FINISHES = "finishes"
    EQUALS = "equals"
    FINISHED_BY = "finished_by"
    CONTAINS = "contains"
    STARTED_BY = "started_by"
    OVERLAPPED_BY = "overlapped_by"
    MET_BY = "met_by"
    AFTER = "after"


@dataclass(frozen=True, slots=True)
class BeatInterval:
    """A half-open beat interval ``[lo, hi)`` on the story (valid-time) axis.

    ``hi=None`` is an open interval (the fact is still in force). All comparisons treat the
    open end as +∞ so an open and a closed interval order correctly.
    """

    lo: int
    hi: int | None = None

    def __post_init__(self) -> None:
        if self.hi is not None and self.hi < self.lo:
            raise ValueError(f"beat interval hi ({self.hi}) < lo ({self.lo})")

    @property
    def open_ended(self) -> bool:
        return self.hi is None

    def _hi(self) -> int:
        return LATEST_BEAT if self.hi is None else self.hi

    def contains(self, beat: int) -> bool:
        """True iff ``beat`` falls in ``[lo, hi)`` (the active-at-beat test, §8.5)."""
        return self.lo <= beat < self._hi() or (self.hi is None and beat >= self.lo)

    def overlaps(self, other: BeatInterval) -> bool:
        """True iff the two intervals share at least one beat (half-open)."""
        return self.lo < other._hi() and other.lo < self._hi()

    def encloses(self, other: BeatInterval) -> bool:
        """True iff ``other`` is wholly inside ``self``."""
        return self.lo <= other.lo and other._hi() <= self._hi()

    def closed_at(self, beat: int) -> BeatInterval:
        """Return a copy with ``hi`` closed at ``beat`` (the forgetting/retire op, §8.5)."""
        return BeatInterval(self.lo, beat)

    def relation(self, other: BeatInterval) -> Allen:
        """Classify how ``self`` relates to ``other`` (the 13 Allen relations)."""
        a_lo, a_hi = self.lo, self._hi()
        b_lo, b_hi = other.lo, other._hi()
        if a_hi < b_lo:
            return Allen.BEFORE
        if a_hi == b_lo:
            return Allen.MEETS
        if a_lo > b_hi:
            return Allen.AFTER
        if a_lo == b_hi:
            return Allen.MET_BY
        if a_lo == b_lo and a_hi == b_hi:
            return Allen.EQUALS
        if a_lo == b_lo:
            return Allen.STARTS if a_hi < b_hi else Allen.STARTED_BY
        if a_hi == b_hi:
            return Allen.FINISHES if a_lo > b_lo else Allen.FINISHED_BY
        if a_lo < b_lo and a_hi > b_hi:
            return Allen.CONTAINS
        if a_lo > b_lo and a_hi < b_hi:
            return Allen.DURING
        if a_lo < b_lo:
            return Allen.OVERLAPS
        return Allen.OVERLAPPED_BY


@dataclass(frozen=True, slots=True)
class TxInterval:
    """A half-open transaction-time interval ``[tx_from, tx_to)`` (UTC).

    ``tx_to=None`` means the row is the current belief. A correction closes ``tx_to`` and
    inserts a successor with ``tx_from`` == that instant, so the tx-axis tiles cleanly.
    """

    tx_from: datetime
    tx_to: datetime | None = None

    def __post_init__(self) -> None:
        # Frozen dataclass: re-set through object.__setattr__ to normalize tz.
        object.__setattr__(self, "tx_from", _as_aware(self.tx_from))
        if self.tx_to is not None:
            tx_to = _as_aware(self.tx_to)
            if tx_to < self.tx_from:
                raise ValueError("tx_to precedes tx_from")
            object.__setattr__(self, "tx_to", tx_to)

    @property
    def current(self) -> bool:
        """True iff this is the still-believed row (``tx_to`` open)."""
        return self.tx_to is None

    def contains(self, instant: datetime) -> bool:
        """True iff the system believed this fact *at* ``instant`` (the as-of-tx test)."""
        instant = _as_aware(instant)
        if instant < self.tx_from:
            return False
        return self.tx_to is None or instant < self.tx_to

    def closed_at(self, instant: datetime) -> TxInterval:
        """Return a copy with ``tx_to`` closed at ``instant`` (a correction)."""
        return TxInterval(self.tx_from, _as_aware(instant))


@dataclass(frozen=True, slots=True)
class BitemporalCoord:
    """A point on the four-dimensional query space: *(branch, beat, as-of-tx)*.

    ``as_of_tx=None`` reads the current belief; a past instant reconstructs what the canon
    believed then. ``beat=LATEST_BEAT`` reads the latest valid version.
    """

    branch: str = "main"
    beat: int = LATEST_BEAT
    as_of_tx: datetime | None = None

    @classmethod
    def now(cls, branch: str = "main", beat: int = LATEST_BEAT) -> BitemporalCoord:
        """A coordinate reading current belief at ``beat`` on ``branch``."""
        return cls(branch=branch, beat=beat, as_of_tx=None)

    def tx_instant(self) -> datetime:
        """The transaction instant this coord reads at (now when open)."""
        return utcnow() if self.as_of_tx is None else _as_aware(self.as_of_tx)


#: The trunk branch every book starts on.
MAIN_BRANCH = "main"


__all__ = [
    "Allen",
    "BeatInterval",
    "BitemporalCoord",
    "LATEST_BEAT",
    "MAIN_BRANCH",
    "TxInterval",
    "utcnow",
]
