"""Conflict-free replicated data types for concurrent canon writes (kinora.md §8).

Multiple reading sessions may edit the canon at the same time — two directors on one book,
or a director editing while the Continuity Supervisor auto-resolves a Critic conflict
(§9.5). We make those writes **commutative** so they merge to the same canon regardless of
order or which replica saw them first. That is the job of this module: pure, dependency-free
CRDT cores plus a Hybrid Logical Clock to stamp every write.

Everything here is **pure and offline-testable** — no DB, no network, no wall-clock
dependence beyond an injectable ``now`` — so the distributed semantics are provable by
unit tests asserting the CRDT laws:

* **commutativity**  ``merge(a, b) == merge(b, a)``
* **associativity**  ``merge(merge(a, b), c) == merge(a, merge(b, c))``
* **idempotence**    ``merge(a, a) == a``

Types
-----
* :class:`HLC` — a Hybrid Logical Clock: monotone, causality-respecting, anchored to
  wall-clock so timestamps stay human-meaningful and two events compare deterministically.
* :class:`Stamp` — ``(HLC, actor_id)`` total order; the tiebreak that makes LWW deterministic.
* :class:`LWWRegister` — last-writer-wins register for a fact's *object value*.
* :class:`ORSet` — observed-remove set for a fact's *existence* (assert vs retire) and for
  set-valued attributes (aliases, reference keys) without lost updates.
* :class:`GCounter` — grow-only counter (per-actor edit tallies / version vectors building block).
* :class:`VersionVector` — per-actor logical clock used to decide fast-forward vs three-way
  merge in :mod:`app.memory.branch_service`.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Generic, TypeVar

V = TypeVar("V")


# --------------------------------------------------------------------------- #
# Hybrid Logical Clock + Stamp
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True, order=True)
class HLC:
    """A Hybrid Logical Clock timestamp.

    ``wall`` is a millisecond UTC epoch (human-meaningful); ``counter`` disambiguates events
    that share a wall-tick, preserving causality even when the physical clock doesn't move.
    Ordering is lexicographic ``(wall, counter)`` — exactly what ``order=True`` gives us.
    """

    wall: int
    counter: int = 0

    def tick(self, now_ms: int) -> HLC:
        """Advance for a *local* event observed at physical time ``now_ms``.

        If physical time moved past ``wall`` we adopt it and reset the counter; otherwise we
        keep ``wall`` and bump the counter (clock didn't advance, but causality must).
        """
        if now_ms > self.wall:
            return HLC(now_ms, 0)
        return HLC(self.wall, self.counter + 1)

    def receive(self, remote: HLC, now_ms: int) -> HLC:
        """Merge a *received* ``remote`` stamp with local time ``now_ms`` (the HLC recv rule).

        The new wall is the max of local time, our wall, and the remote wall; the counter is
        chosen so the result strictly dominates both inputs — guaranteeing monotonicity.
        """
        wall = max(now_ms, self.wall, remote.wall)
        if wall == self.wall == remote.wall:
            counter = max(self.counter, remote.counter) + 1
        elif wall == self.wall:
            counter = self.counter + 1
        elif wall == remote.wall:
            counter = remote.counter + 1
        else:
            counter = 0
        return HLC(wall, counter)


@dataclass(frozen=True, slots=True, order=True)
class Stamp:
    """A total-ordered write stamp: ``(hlc, actor_id)``.

    The ``actor_id`` tiebreak makes the order *total* even for two writes that produced an
    identical HLC on different replicas, so LWW is deterministic everywhere.
    """

    hlc: HLC
    actor_id: str = ""

    def dominates(self, other: Stamp) -> bool:
        """True iff this stamp is strictly the later write."""
        return self > other


class HLCClock:
    """A small stateful HLC generator for one actor (the only stateful thing here).

    Injecting ``now_ms`` keeps it deterministic in tests; in production pass a lambda over
    the wall clock. Not a CRDT itself — it *mints* the stamps the CRDTs order by.
    """

    def __init__(self, actor_id: str, now_ms: Callable[[], int]) -> None:
        self._actor = actor_id
        self._now = now_ms
        self._hlc = HLC(0, 0)

    @property
    def actor_id(self) -> str:
        return self._actor

    def issue(self) -> Stamp:
        """Mint the next local stamp for this actor."""
        self._hlc = self._hlc.tick(self._now())
        return Stamp(self._hlc, self._actor)

    def observe(self, remote: Stamp) -> None:
        """Fold a received remote stamp into local time (keeps the clock monotone)."""
        self._hlc = self._hlc.receive(remote.hlc, self._now())


# --------------------------------------------------------------------------- #
# LWW-Register
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class LWWRegister(Generic[V]):
    """Last-writer-wins register. ``merge`` keeps the value with the higher :class:`Stamp`.

    Used for a fact's *object value* (and any scalar attribute): two concurrent edits resolve
    deterministically to the latest stamp on every replica, so no write is "lost" ambiguously
    — the loser is simply ordered before the winner.
    """

    value: V
    stamp: Stamp

    def set(self, value: V, stamp: Stamp) -> LWWRegister[V]:
        """Apply a local write; takes effect only if ``stamp`` is >= the current one."""
        return LWWRegister(value, stamp) if stamp >= self.stamp else self

    def merge(self, other: LWWRegister[V]) -> LWWRegister[V]:
        """Conflict-free merge: the higher stamp wins (total order ⇒ deterministic)."""
        if other.stamp > self.stamp:
            return other
        return self


# --------------------------------------------------------------------------- #
# OR-Set (observed-remove set)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ORSet(Generic[V]):
    """An observed-remove set: add and remove commute without a lost update.

    Each *add* of an element carries a unique tag; a *remove* tombstones exactly the tags it
    has observed. Concurrent ``add(x)`` on one replica and ``remove(x)`` on another therefore
    merge to "x present" (the add's fresh tag was never observed by the remove) — the
    add-wins bias the canon wants: a concurrently-reasserted fact is not silently dropped.

    Internally two tag→element maps: ``adds`` and ``tombstones``. An element is *present* iff
    it has an add tag not in tombstones.
    """

    adds: frozenset[tuple[str, V]] = field(default_factory=frozenset)
    tombstones: frozenset[str] = field(default_factory=frozenset)

    def add(self, element: V, tag: str) -> ORSet[V]:
        """Add ``element`` with a globally-unique ``tag`` (e.g. an actor-scoped stamp str)."""
        return ORSet(self.adds | {(tag, element)}, self.tombstones)

    def remove(self, element: V) -> ORSet[V]:
        """Remove ``element`` by tombstoning every tag currently observed for it."""
        observed = {tag for (tag, el) in self.adds if el == element}
        return ORSet(self.adds, self.tombstones | observed)

    def elements(self) -> set[V]:
        """The live elements (added by some tag not yet tombstoned)."""
        return {el for (tag, el) in self.adds if tag not in self.tombstones}

    def contains(self, element: V) -> bool:
        return element in self.elements()

    def merge(self, other: ORSet[V]) -> ORSet[V]:
        """Conflict-free merge: union the adds, union the tombstones (state-based / CvRDT)."""
        return ORSet(self.adds | other.adds, self.tombstones | other.tombstones)


# --------------------------------------------------------------------------- #
# G-Counter
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GCounter:
    """A grow-only counter: per-actor tallies, value = Σ, merge = per-actor max.

    Used for monotone edit counts and as the building block of :class:`VersionVector`.
    """

    counts: dict[str, int] = field(default_factory=dict)

    def increment(self, actor_id: str, by: int = 1) -> GCounter:
        if by < 0:
            raise ValueError("GCounter only grows")
        nxt = dict(self.counts)
        nxt[actor_id] = nxt.get(actor_id, 0) + by
        return GCounter(nxt)

    def value(self) -> int:
        return sum(self.counts.values())

    def merge(self, other: GCounter) -> GCounter:
        keys = set(self.counts) | set(other.counts)
        return GCounter({k: max(self.counts.get(k, 0), other.counts.get(k, 0)) for k in keys})


# --------------------------------------------------------------------------- #
# Version Vector (causality for fork/diff/merge)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class VersionVector:
    """A per-actor logical clock for deciding fast-forward vs concurrent merge.

    ``observe(actor)`` records that this branch has seen one more write from ``actor``.
    Two vectors are *comparable* when one dominates the other elementwise (fast-forward);
    otherwise the branches are *concurrent* and a three-way merge rule is needed.
    """

    clock: dict[str, int] = field(default_factory=dict)

    def observe(self, actor_id: str, count: int = 1) -> VersionVector:
        nxt = dict(self.clock)
        nxt[actor_id] = nxt.get(actor_id, 0) + count
        return VersionVector(nxt)

    def get(self, actor_id: str) -> int:
        return self.clock.get(actor_id, 0)

    def dominates(self, other: VersionVector) -> bool:
        """True iff ``self >= other`` on every actor (``self`` saw everything ``other`` did)."""
        keys = set(self.clock) | set(other.clock)
        return all(self.get(k) >= other.get(k) for k in keys)

    def concurrent_with(self, other: VersionVector) -> bool:
        """True iff neither dominates the other (genuine concurrency ⇒ needs a merge rule)."""
        return not self.dominates(other) and not other.dominates(self)

    def merge(self, other: VersionVector) -> VersionVector:
        """Least upper bound: elementwise max."""
        keys = set(self.clock) | set(other.clock)
        return VersionVector({k: max(self.get(k), other.get(k)) for k in keys})


def fold_merge(items: Iterable[V], merge: Callable[[V, V], V]) -> V:
    """Fold a non-empty iterable of CRDT states with ``merge`` (associativity test helper)."""
    it = iter(items)
    try:
        acc = next(it)
    except StopIteration as exc:  # pragma: no cover - guarded by callers
        raise ValueError("fold_merge over empty iterable") from exc
    for item in it:
        acc = merge(acc, item)
    return acc


def all_orderings_agree(states: list[V], merge: Callable[[V, V], V]) -> bool:
    """True iff merging ``states`` in *every* permutation yields the same result.

    A direct, brute-force commutativity+associativity check used by the CRDT-law tests for
    small state sets (the convergence guarantee a CvRDT must satisfy).
    """
    results = [fold_merge(list(perm), merge) for perm in itertools.permutations(states)]
    first = results[0]
    return all(r == first for r in results)


def with_actor(vv: VersionVector, actor_id: str) -> VersionVector:
    """Tiny helper kept for symmetry with :func:`dataclasses.replace` call sites."""
    return replace(vv, clock={**vv.clock, actor_id: vv.get(actor_id) + 1})


__all__ = [
    "GCounter",
    "HLC",
    "HLCClock",
    "LWWRegister",
    "ORSet",
    "Stamp",
    "VersionVector",
    "all_orderings_agree",
    "fold_merge",
    "with_actor",
]
