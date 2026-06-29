"""Pluggable conflict resolution for active-active replication.

When two regions write the same key concurrently the replicas must converge to
*one* value, deterministically, regardless of the order updates arrive. This
module provides the resolution strategies and the registry that binds a key (or
key class) to one:

* :class:`LWWResolver` — last-writer-wins by :class:`HybridTimestamp`. The
  timestamp total order (with its node tiebreak) makes the winner identical on
  every replica. Simple, lossy-by-design.
* :class:`GCounterValue` / :class:`PNCounterValue` — grow-only and
  positive-negative counters. Merge is the pointwise max of per-node tallies, so
  concurrent increments *all* survive (no lost update).
* :class:`LWWRegisterValue` — a register whose merge keeps the higher-stamped
  write; the resolver form of LWW for embedding inside a structured value.
* :class:`ORSetValue` — observed-remove set: concurrent add/remove of the same
  element resolves *add-wins*, and no add is silently dropped.
* :class:`MVRegisterValue` — multi-value register: concurrent writes are *kept*
  as a sibling set (the "don't resolve, surface it" strategy) and collapse only
  when a later write causally dominates all siblings.
* :class:`CustomResolver` — wraps an app-supplied associative/commutative merge
  function (with a determinism guard) for domain values the generic CRDTs don't
  capture.

Every resolver implements :class:`ConflictResolver` and guarantees the algebraic
laws the simulator and property tests assert:

* **commutativity**  ``resolve(a, b) == resolve(b, a)``
* **associativity**  ``resolve(resolve(a, b), c) == resolve(a, resolve(b, c))``
* **idempotence**    ``resolve(a, a) == a``

These three laws are exactly the conditions under which an eventually-consistent
store provably converges, so they are not decoration — they are the contract.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.version import VersionVector

V = TypeVar("V")
T = TypeVar("T")


class ConflictResolver(ABC, Generic[V]):
    """Strategy that merges two concurrent values for one key into a winner.

    Implementations MUST be commutative, associative, and idempotent over the
    value domain. ``resolve`` receives two *already-decoded* values; the protocol
    calls it whenever it must reconcile divergent state for a key.

    ``timestamped`` declares the resolution mode the store must use:

    * ``True`` — the resolver decides by *write timestamp* (LWW family). The
      store knows each cell's :class:`HybridTimestamp` already, so for these it
      resolves directly by the timestamp total order and the raw value is opaque
      (it need not be a :class:`Stamped`). ``resolve`` still works if you hand it
      :class:`Stamped` values, for use outside the store.
    * ``False`` — the resolver is a *state-based* CRDT join over the values
      themselves (counters, OR-sets); the store calls ``resolve`` on the raw
      values and the timestamp is irrelevant to the merge.
    """

    #: Whether the store should resolve by cell timestamp (see class docstring).
    timestamped: bool = False

    @abstractmethod
    def resolve(self, left: V, right: V) -> V:
        """Merge ``left`` and ``right`` into the converged value."""
        raise NotImplementedError

    def name(self) -> str:
        return type(self).__name__


# --------------------------------------------------------------------------- #
# Last-writer-wins
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Stamped(Generic[T]):
    """A value carrying the :class:`HybridTimestamp` of the write that set it."""

    value: T
    timestamp: HybridTimestamp


class LWWResolver(ConflictResolver[Stamped[T]]):
    """Last-writer-wins over :class:`Stamped` values.

    The winner is the value with the higher :class:`HybridTimestamp`. Because
    that order is total (node tiebreak), ties cannot happen across distinct
    nodes and the choice is deterministic on every replica.
    """

    timestamped = True

    def resolve(self, left: Stamped[T], right: Stamped[T]) -> Stamped[T]:
        return right if right.timestamp > left.timestamp else left


# Alias for embedding LWW as a value type inside structured documents.
LWWRegisterValue = Stamped


# --------------------------------------------------------------------------- #
# Counters
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class GCounterValue:
    """A grow-only counter: per-node tallies, value is their sum.

    Merge is the pointwise max, which preserves every node's contribution under
    concurrency — the canonical lost-update-free counter.
    """

    tallies: Mapping[NodeId, int] = field(default_factory=dict)

    @property
    def value(self) -> int:
        return sum(self.tallies.values())

    def increment(self, node: NodeId, amount: int = 1) -> GCounterValue:
        if amount < 0:
            raise ValueError("GCounter only grows; use PNCounter for decrements")
        updated = dict(self.tallies)
        updated[node] = updated.get(node, 0) + amount
        return GCounterValue(updated)

    def merge(self, other: GCounterValue) -> GCounterValue:
        merged = dict(self.tallies)
        for node, count in other.tallies.items():
            if count > merged.get(node, 0):
                merged[node] = count
        return GCounterValue(merged)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, GCounterValue):
            return NotImplemented
        nonzero = {n: c for n, c in self.tallies.items() if c}
        other_nonzero = {n: c for n, c in other.tallies.items() if c}
        return nonzero == other_nonzero

    def __hash__(self) -> int:
        return hash(frozenset((n, c) for n, c in self.tallies.items() if c))


@dataclass(frozen=True, slots=True)
class PNCounterValue:
    """A positive-negative counter: two G-counters, value is ``P - N``."""

    positive: GCounterValue = field(default_factory=GCounterValue)
    negative: GCounterValue = field(default_factory=GCounterValue)

    @property
    def value(self) -> int:
        return self.positive.value - self.negative.value

    def add(self, node: NodeId, amount: int) -> PNCounterValue:
        if amount >= 0:
            return PNCounterValue(self.positive.increment(node, amount), self.negative)
        return PNCounterValue(self.positive, self.negative.increment(node, -amount))

    def merge(self, other: PNCounterValue) -> PNCounterValue:
        return PNCounterValue(
            self.positive.merge(other.positive),
            self.negative.merge(other.negative),
        )


class GCounterResolver(ConflictResolver[GCounterValue]):
    def resolve(self, left: GCounterValue, right: GCounterValue) -> GCounterValue:
        return left.merge(right)


class PNCounterResolver(ConflictResolver[PNCounterValue]):
    def resolve(self, left: PNCounterValue, right: PNCounterValue) -> PNCounterValue:
        return left.merge(right)


# --------------------------------------------------------------------------- #
# Observed-remove set
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ORSetValue(Generic[T]):
    """An observed-remove set: add-wins under concurrent add/remove.

    Each element maps to the set of unique *add tags* currently live for it; a
    remove drops only the tags it has observed. Concurrent ``add`` (fresh tag)
    therefore survives a concurrent ``remove`` — the add-wins bias. Merge unions
    the live tags and unions the tombstones, then live-minus-tombstoned wins.
    """

    adds: Mapping[T, frozenset[str]] = field(default_factory=dict)
    tombstones: Mapping[T, frozenset[str]] = field(default_factory=dict)

    def elements(self) -> frozenset[T]:
        live = []
        for element, tags in self.adds.items():
            remaining = tags - self.tombstones.get(element, frozenset())
            if remaining:
                live.append(element)
        return frozenset(live)

    def contains(self, element: T) -> bool:
        return element in self.elements()

    def add(self, element: T, tag: str) -> ORSetValue[T]:
        adds = dict(self.adds)
        adds[element] = adds.get(element, frozenset()) | {tag}
        return ORSetValue(adds, self.tombstones)

    def remove(self, element: T) -> ORSetValue[T]:
        """Tombstone every tag currently observed for ``element`` (observed-remove)."""
        observed = self.adds.get(element, frozenset())
        if not observed:
            return self
        tombstones = dict(self.tombstones)
        tombstones[element] = tombstones.get(element, frozenset()) | observed
        return ORSetValue(self.adds, tombstones)

    def merge(self, other: ORSetValue[T]) -> ORSetValue[T]:
        adds: dict[T, frozenset[str]] = {}
        for element in set(self.adds) | set(other.adds):
            adds[element] = self.adds.get(element, frozenset()) | other.adds.get(
                element, frozenset()
            )
        tombstones: dict[T, frozenset[str]] = {}
        for element in set(self.tombstones) | set(other.tombstones):
            tombstones[element] = self.tombstones.get(
                element, frozenset()
            ) | other.tombstones.get(element, frozenset())
        return ORSetValue(adds, tombstones)

    @staticmethod
    def _canonical(m: Mapping[T, frozenset[str]]) -> frozenset[tuple[T, frozenset[str]]]:
        return frozenset((e, tags) for e, tags in m.items() if tags)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ORSetValue):
            return NotImplemented
        return self._canonical(self.adds) == self._canonical(
            other.adds
        ) and self._canonical(self.tombstones) == self._canonical(other.tombstones)

    def __hash__(self) -> int:
        return hash((self._canonical(self.adds), self._canonical(self.tombstones)))


class ORSetResolver(ConflictResolver[ORSetValue[T]]):
    def resolve(self, left: ORSetValue[T], right: ORSetValue[T]) -> ORSetValue[T]:
        return left.merge(right)


# --------------------------------------------------------------------------- #
# Multi-value register
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Dot:
    """A globally-unique write event identity: ``(node, seq)``.

    Each :meth:`MVRegisterValue.write` mints a fresh dot. Dots are what the
    causal context tracks, so "have I seen this write?" is a vector membership
    test rather than a timestamp comparison (which would be wrong — see below).
    """

    node: NodeId
    seq: int


@dataclass(frozen=True, slots=True)
class MVSibling(Generic[T]):
    """One live value of a multi-value register, tagged with its causal dot."""

    value: T
    dot: Dot
    timestamp: HybridTimestamp


@dataclass(frozen=True, slots=True)
class MVRegisterValue(Generic[T]):
    """A multi-value (LWW-element-free) register that *keeps* concurrent writes.

    The classic correctness trap is to discard a sibling because another has a
    higher :class:`HybridTimestamp` — but HLC stamps are a *total* order, so that
    would silently drop genuinely-concurrent writes. We instead track causality
    explicitly: each sibling carries a :class:`Dot` and the register carries a
    ``context`` :class:`VersionVector` of every dot ever observed. A sibling is
    obsolete iff its dot is covered by the context of a *causally newer* write
    (i.e. some write happened-after it). Concurrent writes — neither in the
    other's context — coexist until a write that has seen both supersedes them.

    :attr:`values` is the set of live sibling values; :attr:`is_conflicted` is
    true when more than one survives.
    """

    siblings: frozenset[MVSibling[T]] = field(default_factory=frozenset)
    context: VersionVector = field(default_factory=VersionVector.empty)

    @property
    def values(self) -> frozenset[T]:
        return frozenset(s.value for s in self.siblings)

    @property
    def is_conflicted(self) -> bool:
        return len(self.siblings) > 1

    def write(
        self,
        value: T,
        timestamp: HybridTimestamp,
        *,
        seq: int | None = None,
    ) -> MVRegisterValue[T]:
        """Write ``value`` observed at ``timestamp`` from ``timestamp.node``.

        ``seq`` is the writer's per-node event sequence; if omitted it is derived
        from the current context (the next dot for that node). The write's causal
        context is the register's current context plus its own dot, so it
        supersedes exactly the siblings it has observed and coexists with any it
        has not.
        """
        node = timestamp.node
        dot_seq = seq if seq is not None else self.context.get(node) + 1
        dot = Dot(node, dot_seq)
        new_context = self.context.advanced(node, dot_seq)
        # A prior sibling is superseded iff its dot is now in the context AND it
        # is not this same dot. Because we are adding a write that observed the
        # whole current context, every existing sibling is superseded by it.
        survivors = frozenset(
            s for s in self.siblings if not new_context.includes(s.dot.node, s.dot.seq)
        )
        survivors |= {MVSibling(value, dot, timestamp)}
        return MVRegisterValue(survivors, new_context)

    def merge(self, other: MVRegisterValue[T]) -> MVRegisterValue[T]:
        """Causal merge: keep a sibling unless the *other* side's context covers its dot."""
        merged_context = self.context.merge(other.context)
        kept: set[MVSibling[T]] = set()
        for sib in self.siblings:
            # Drop iff the other replica has causally seen-past this dot.
            if not other.context.includes(sib.dot.node, sib.dot.seq) or sib in other.siblings:
                kept.add(sib)
        for sib in other.siblings:
            if not self.context.includes(sib.dot.node, sib.dot.seq) or sib in self.siblings:
                kept.add(sib)
        return MVRegisterValue(frozenset(kept), merged_context)


class MVRegisterResolver(ConflictResolver[MVRegisterValue[T]]):
    def resolve(self, left: MVRegisterValue[T], right: MVRegisterValue[T]) -> MVRegisterValue[T]:
        return left.merge(right)


# --------------------------------------------------------------------------- #
# App-defined merge
# --------------------------------------------------------------------------- #


class CustomResolver(ConflictResolver[V]):
    """Wraps an app-supplied merge function for a domain value type.

    The caller promises ``merge`` is commutative, associative, and idempotent.
    ``strict`` (default) runs a cheap self-consistency check on each call —
    ``merge(a, a) == a`` and ``merge(a, b) == merge(b, a)`` — and raises
    :class:`NonConvergentMergeError` if the supplied function violates them, so a
    buggy resolver fails loudly in tests instead of silently diverging in prod.
    """

    def __init__(
        self,
        merge: Callable[[V, V], V],
        *,
        label: str = "custom",
        strict: bool = True,
    ) -> None:
        self._merge = merge
        self._label = label
        self._strict = strict

    def resolve(self, left: V, right: V) -> V:
        merged = self._merge(left, right)
        if self._strict:
            if self._merge(right, left) != merged:
                raise NonConvergentMergeError(f"{self._label}: merge is not commutative")
            if self._merge(merged, merged) != merged:
                raise NonConvergentMergeError(f"{self._label}: merge is not idempotent")
        return merged

    def name(self) -> str:
        return f"custom:{self._label}"


class NonConvergentMergeError(RuntimeError):
    """Raised when a :class:`CustomResolver`'s merge violates the convergence laws."""


# --------------------------------------------------------------------------- #
# Resolver registry
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class KeyClass:
    """A predicate-named binding target: keys matching ``prefix`` use a resolver.

    Bindings are tried longest-prefix-first so a specific class wins over a
    broader one; an empty prefix is the catch-all default.
    """

    prefix: str

    def matches(self, key: str) -> bool:
        return key.startswith(self.prefix)


class ResolverRegistry:
    """Maps keys to their :class:`ConflictResolver` by longest-prefix match.

    Register specific bindings (``"counter:"`` → counter resolver) plus a default
    (empty prefix). :meth:`for_key` resolves the most specific binding. This is
    how one store hosts heterogeneous value types — LWW scalars, CRDT counters,
    OR-sets — each converging by its own correct rule.
    """

    def __init__(self, default: ConflictResolver[Any] | None = None) -> None:
        self._bindings: list[tuple[KeyClass, ConflictResolver[Any]]] = []
        if default is not None:
            self._bindings.append((KeyClass(""), default))

    def register(self, prefix: str, resolver: ConflictResolver[Any]) -> None:
        self._bindings.append((KeyClass(prefix), resolver))
        # Longest prefix first so the most specific class wins.
        self._bindings.sort(key=lambda b: len(b[0].prefix), reverse=True)

    def for_key(self, key: str) -> ConflictResolver[Any]:
        for key_class, resolver in self._bindings:
            if key_class.matches(key):
                return resolver
        raise KeyError(f"no conflict resolver bound for key {key!r}")

    def resolve(self, key: str, left: object, right: object) -> object:
        return self.for_key(key).resolve(left, right)
