"""Shard-key strategies: map a :class:`ShardKey` to the shard(s) that own it.

A *strategy* is the pluggable policy that turns a key into a placement. Four
shapes cover the field:

* :class:`ModuloHashStrategy` — ``hash(key) % N``. Dead simple, perfectly even,
  but reshards badly (changing ``N`` remaps almost every key).
* :class:`RangeStrategy` — ordered bounds (``[lo, hi)`` per shard). Keeps ranges
  scannable and lets a hot range be split, at the cost of needing a balanced
  bound table.
* :class:`DirectoryStrategy` — an explicit ``key → shard`` lookup table (with a
  fallback strategy for unmapped keys). The most flexible — individual hot keys
  can be pinned/moved — at the cost of a lookup table to keep.
* :class:`ConsistentHashStrategy` — a hash ring with **virtual nodes**. Adding or
  removing a shard remaps only ``~1/N`` of keys (not all of them), so it is the
  resharding-friendly default. Weights and vnode count tune balance.

Every strategy implements :meth:`route_one` (key → single owning shard id) and,
where meaningful, :meth:`route_range` (a key range → the set of shard ids it
spans, for the planner's scatter set). Strategies are *pure functions of the
topology*: same topology + same key ⇒ same shard, in any process. They never
touch a connection.
"""

from __future__ import annotations

import bisect
import hashlib
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.datascale.sharding.keys import ShardKey, ShardKeyValue
from app.datascale.sharding.topology import ShardState, ShardTopology


class RoutingError(RuntimeError):
    """Raised when a key cannot be routed (no shard, or target not routable)."""


@dataclass(frozen=True, slots=True)
class StrategyKind:
    """Human-readable identifier of a strategy flavour (for logging/metrics)."""

    name: str


class ShardStrategy(ABC):
    """Abstract shard-placement policy.

    A strategy is bound to a :class:`ShardTopology` and answers *which* shard a
    key belongs to. It does **not** consider shard state for the placement
    decision (that is policy the router layers on, e.g. "writes only to ACTIVE
    shards"); a strategy answers the logical question "where does this key live"
    so resharding can reason about it independent of momentary state.
    """

    kind: StrategyKind

    @abstractmethod
    def route_one(self, key: ShardKey) -> str:
        """Return the id of the single shard that owns ``key``."""

    def route_range(
        self, low: ShardKeyValue | None, high: ShardKeyValue | None
    ) -> tuple[str, ...]:
        """Return the shard ids a half-open key range ``[low, high)`` spans.

        Default: a range query cannot be narrowed (hash/directory scatter the
        keyspace arbitrarily) so it spans *every* shard. Range strategies
        override this to prune to the touched shards.
        """
        return self.all_shards()

    @abstractmethod
    def all_shards(self) -> tuple[str, ...]:
        """Every shard id this strategy may route to (the full scatter set)."""

    def describe(self) -> Mapping[str, object]:
        """A JSON-friendly description (for the planner's EXPLAIN + metrics)."""
        return {"kind": self.kind.name, "shards": list(self.all_shards())}


# --------------------------------------------------------------------------- #
# Modulo-hash
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ModuloHashStrategy(ShardStrategy):
    """``hash(key) % N`` over an ordered shard list.

    The shard list order *is* the bucket assignment (bucket ``i`` → ``shards[i]``)
    so the mapping is reproducible. Buckets are assigned over **all** shards in
    the topology (not just ACTIVE ones) so a key's bucket is stable even while a
    shard is transiently READ_ONLY mid-reshard.
    """

    topology: ShardTopology
    algo: str = "sha1"
    kind: StrategyKind = field(default=StrategyKind("modulo_hash"), init=False)

    def __post_init__(self) -> None:
        if len(self.topology) == 0:
            raise ValueError("ModuloHashStrategy needs a non-empty topology")

    def _buckets(self) -> tuple[str, ...]:
        return self.topology.ids

    def route_one(self, key: ShardKey) -> str:
        buckets = self._buckets()
        idx = key.hashed_mod(len(buckets), algo=self.algo)
        return buckets[idx]

    def all_shards(self) -> tuple[str, ...]:
        return self._buckets()


# --------------------------------------------------------------------------- #
# Range
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RangeBound:
    """One half-open range ``[lower, upper)`` owned by ``shard_id``.

    ``lower=None`` means "−∞" (the first range); ``upper=None`` means "+∞" (the
    last range). Bounds must be contiguous and non-overlapping across a
    :class:`RangeStrategy` — validated at construction.
    """

    shard_id: str
    lower: ShardKeyValue | None
    upper: ShardKeyValue | None


@dataclass(frozen=True, slots=True)
class RangeStrategy(ShardStrategy):
    """Ordered, contiguous range bounds mapping a comparable key to a shard.

    Bounds are sorted by ``lower`` and must tile the keyspace with no gaps or
    overlaps (so every key lands in exactly one range). Range queries prune to
    only the shards whose ranges intersect the query interval — the whole point
    of range sharding (keep ordered scans local).

    Keys must be mutually comparable (all ints, or all strings, …); a range
    strategy on heterogeneous types is a configuration error surfaced at routing
    time as a :class:`TypeError`.
    """

    bounds: tuple[RangeBound, ...]
    kind: StrategyKind = field(default=StrategyKind("range"), init=False)
    #: Cached lower edges for bisection (built in ``__post_init__``).
    _lowers: tuple[object, ...] = field(default=(), init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.bounds:
            raise ValueError("RangeStrategy needs at least one bound")
        ordered = sorted(self.bounds, key=_lower_sort_key)
        # Contiguity / non-overlap validation.
        for i, bound in enumerate(ordered):
            if i == 0 and bound.lower is not None:
                raise ValueError("first range must start at -inf (lower=None)")
            if i == len(ordered) - 1 and bound.upper is not None:
                raise ValueError("last range must end at +inf (upper=None)")
            if (
                bound.lower is not None
                and bound.upper is not None
                and bound.lower >= bound.upper  # type: ignore[operator]
            ):
                raise ValueError(
                    f"range {bound.shard_id!r}: lower {bound.lower!r} "
                    f">= upper {bound.upper!r}"
                )
            if i > 0:
                prev = ordered[i - 1]
                if prev.upper != bound.lower:
                    raise ValueError(
                        "ranges must be contiguous: "
                        f"{prev.shard_id!r} ends at {prev.upper!r} but "
                        f"{bound.shard_id!r} starts at {bound.lower!r}"
                    )
        object.__setattr__(self, "bounds", tuple(ordered))
        # Lower edges (skip the -inf of the first range) for bisect_right.
        object.__setattr__(
            self, "_lowers", tuple(b.lower for b in ordered[1:])
        )

    def _index_for(self, value: ShardKeyValue) -> int:
        """Index of the range owning ``value`` via binary search on lower edges."""
        # ``_lowers`` holds the lower edge of ranges 1..n. bisect_right finds the
        # first edge strictly greater than value; the owning range is that index.
        return bisect.bisect_right(self._lowers, value)  # type: ignore[type-var]

    def route_one(self, key: ShardKey) -> str:
        value = key.single_value
        return self.bounds[self._index_for(value)].shard_id

    def route_range(
        self, low: ShardKeyValue | None, high: ShardKeyValue | None
    ) -> tuple[str, ...]:
        """Shards whose ranges intersect ``[low, high)`` — ordered, deduplicated."""
        start = 0 if low is None else self._index_for(low)
        if high is None:
            end = len(self.bounds) - 1
        else:
            # ``high`` is exclusive; find the last range that *contains* a key
            # < high. If high lands exactly on a lower edge, that higher range
            # is not touched.
            idx = self._index_for(high)
            end = idx
            if idx < len(self.bounds) and self.bounds[idx].lower == high:
                end = idx - 1
            if end < start:
                end = start
        out: list[str] = []
        seen: set[str] = set()
        for bound in self.bounds[start : end + 1]:
            if bound.shard_id not in seen:
                seen.add(bound.shard_id)
                out.append(bound.shard_id)
        return tuple(out)

    def all_shards(self) -> tuple[str, ...]:
        out: list[str] = []
        seen: set[str] = set()
        for bound in self.bounds:
            if bound.shard_id not in seen:
                seen.add(bound.shard_id)
                out.append(bound.shard_id)
        return tuple(out)

    def describe(self) -> Mapping[str, object]:
        return {
            "kind": self.kind.name,
            "bounds": [
                {"shard": b.shard_id, "lower": b.lower, "upper": b.upper}
                for b in self.bounds
            ],
        }


def _lower_sort_key(bound: RangeBound) -> tuple[int, object]:
    """Sort key placing ``lower=None`` (−∞) first."""
    if bound.lower is None:
        return (0, b"")
    return (1, bound.lower)


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class DirectoryStrategy(ShardStrategy):
    """Explicit ``key → shard`` table with a fallback strategy for misses.

    The directory pins specific keys to specific shards — exactly what online
    *move* of a single tenant needs, and what lets a hot key be isolated onto its
    own shard. Keys absent from the table fall through to ``fallback`` (commonly
    a :class:`ConsistentHashStrategy`), so the directory only stores the
    *exceptions*. With no fallback, an unmapped key raises :class:`RoutingError`.
    """

    table: Mapping[ShardKey, str]
    fallback: ShardStrategy | None = None
    #: All shard ids reachable (table targets ∪ fallback's). Cached.
    _all: tuple[str, ...] = field(default=(), init=False, repr=False)
    kind: StrategyKind = field(default=StrategyKind("directory"), init=False)

    def __post_init__(self) -> None:
        ids: list[str] = []
        seen: set[str] = set()
        for shard_id in self.table.values():
            if shard_id not in seen:
                seen.add(shard_id)
                ids.append(shard_id)
        if self.fallback is not None:
            for shard_id in self.fallback.all_shards():
                if shard_id not in seen:
                    seen.add(shard_id)
                    ids.append(shard_id)
        object.__setattr__(self, "_all", tuple(ids))

    def route_one(self, key: ShardKey) -> str:
        pinned = self.table.get(key)
        if pinned is not None:
            return pinned
        if self.fallback is not None:
            return self.fallback.route_one(key)
        raise RoutingError(f"no directory entry for {key} and no fallback strategy")

    def all_shards(self) -> tuple[str, ...]:
        return self._all

    def with_entry(self, key: ShardKey, shard_id: str) -> DirectoryStrategy:
        """Return a copy that pins ``key`` to ``shard_id`` (online move primitive)."""
        new_table = dict(self.table)
        new_table[key] = shard_id
        return DirectoryStrategy(table=new_table, fallback=self.fallback)

    def without_entry(self, key: ShardKey) -> DirectoryStrategy:
        """Return a copy with ``key`` un-pinned (falls back to ``fallback``)."""
        new_table = dict(self.table)
        new_table.pop(key, None)
        return DirectoryStrategy(table=new_table, fallback=self.fallback)

    def describe(self) -> Mapping[str, object]:
        return {
            "kind": self.kind.name,
            "entries": len(self.table),
            "fallback": self.fallback.describe() if self.fallback else None,
        }


# --------------------------------------------------------------------------- #
# Consistent hashing with virtual nodes
# --------------------------------------------------------------------------- #


def _ring_hash(token: str, algo: str) -> int:
    """A stable 64-bit ring position for a token string."""
    digest = hashlib.new(algo, token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True, slots=True)
class ConsistentHashStrategy(ShardStrategy):
    """A hash ring with virtual nodes — the resharding-friendly default.

    Each shard places ``vnodes * weight`` points on a 64-bit ring (the vnode
    token is ``"{shard_id}#{i}"``). A key is hashed onto the ring and owned by
    the first vnode at-or-after it (wrapping past the top). Because the ring is
    dense and per-shard, adding or removing a shard only moves the keys that fall
    in the arcs the changed vnodes covered — roughly ``1/N`` of the keyspace —
    instead of remapping everything the way modulo-hash does.

    ``vnodes`` trades balance for memory: more vnodes ⇒ smoother key
    distribution and smaller variance, at a larger ring. 128–256 is a sane
    default. ``weight`` scales a shard's vnode count so a bigger box owns
    proportionally more of the ring.
    """

    topology: ShardTopology
    vnodes: int = 128
    algo: str = "sha1"
    #: Sorted ring positions and the parallel shard-id array (built in init).
    _ring_positions: tuple[int, ...] = field(default=(), init=False, repr=False)
    _ring_owners: tuple[str, ...] = field(default=(), init=False, repr=False)
    kind: StrategyKind = field(default=StrategyKind("consistent_hash"), init=False)

    def __post_init__(self) -> None:
        if len(self.topology) == 0:
            raise ValueError("ConsistentHashStrategy needs a non-empty topology")
        if self.vnodes < 1:
            raise ValueError("vnodes must be >= 1")
        ring: list[tuple[int, str]] = []
        for shard in self.topology.shards:
            count = self.vnodes * shard.weight
            for i in range(count):
                pos = _ring_hash(f"{shard.id}#{i}", self.algo)
                ring.append((pos, shard.id))
        # Deterministic ordering, with shard id breaking position ties so a hash
        # collision between two vnodes resolves identically in every process.
        ring.sort(key=lambda pair: (pair[0], pair[1]))
        object.__setattr__(self, "_ring_positions", tuple(p for p, _ in ring))
        object.__setattr__(self, "_ring_owners", tuple(s for _, s in ring))

    def route_one(self, key: ShardKey) -> str:
        pos = key.hashed(algo=self.algo) % (1 << 64)
        idx = bisect.bisect_left(self._ring_positions, pos)
        if idx == len(self._ring_positions):
            idx = 0  # wrap around the ring
        return self._ring_owners[idx]

    def route_replicas(self, key: ShardKey, count: int) -> tuple[str, ...]:
        """The first ``count`` *distinct* shards clockwise from the key.

        This is the consistent-hashing replica-placement primitive: walk the
        ring from the key's position, collecting distinct shard ids until we have
        ``count`` of them (or run out of shards). Useful for N-way replication or
        a "fallback shard" on a primary-down read.
        """
        if count < 1:
            raise ValueError("count must be >= 1")
        pos = key.hashed(algo=self.algo) % (1 << 64)
        start = bisect.bisect_left(self._ring_positions, pos)
        out: list[str] = []
        seen: set[str] = set()
        n = len(self._ring_positions)
        for step in range(n):
            owner = self._ring_owners[(start + step) % n]
            if owner not in seen:
                seen.add(owner)
                out.append(owner)
                if len(out) == count:
                    break
        return tuple(out)

    def all_shards(self) -> tuple[str, ...]:
        return self.topology.ids

    def ring_size(self) -> int:
        """Total vnode count on the ring (for balance diagnostics)."""
        return len(self._ring_positions)

    def describe(self) -> Mapping[str, object]:
        return {
            "kind": self.kind.name,
            "vnodes": self.vnodes,
            "ring_size": self.ring_size(),
            "shards": list(self.all_shards()),
        }


def ownership_distribution(
    strategy: ShardStrategy, keys: Sequence[ShardKey]
) -> dict[str, int]:
    """Count how many of ``keys`` each shard owns (balance diagnostics / tests)."""
    counts: dict[str, int] = dict.fromkeys(strategy.all_shards(), 0)
    for key in keys:
        sid = strategy.route_one(key)
        counts[sid] = counts.get(sid, 0) + 1
    return counts


def routable_state(state: ShardState, *, write: bool) -> bool:
    """Whether a shard in ``state`` may serve the given access (write/read)."""
    return state.accepts_writes if write else state.accepts_reads


__all__ = [
    "ConsistentHashStrategy",
    "DirectoryStrategy",
    "ModuloHashStrategy",
    "RangeBound",
    "RangeStrategy",
    "RoutingError",
    "ShardStrategy",
    "StrategyKind",
    "ownership_distribution",
    "routable_state",
]
