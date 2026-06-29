"""Shard topology: the description of the shard fleet a strategy routes across.

A :class:`Shard` is one physical destination — a logical id plus the connection
coordinates (primary URL + optional replica URL) the connection proxy and
:class:`~app.db.engine.EngineRegistry` consume. A :class:`ShardTopology` is the
immutable set of shards plus per-shard *state* (a shard being split or drained is
not a routing target the same way a live shard is).

The topology is deliberately data-only and side-effect-free: it opens no
sockets, builds no engines. The router resolves a key to a :class:`Shard`; the
*connection proxy* (:mod:`app.datascale.sharding.proxy`) is what later turns a
:class:`Shard` into a live, pooled connection. Keeping them separate means a
plan can be computed, logged and tested entirely without infrastructure.

Shard *states* drive resharding safety:

* ``ACTIVE`` — serves reads and writes normally.
* ``READ_ONLY`` — serves reads; writes are rejected (a source shard mid-cutover).
* ``DRAINING`` — being emptied (a split source after cutover); reads allowed
  until the directory no longer points here.
* ``OFFLINE`` — administratively down; routing here raises.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace


class ShardState(enum.Enum):
    """Lifecycle state of a shard (drives resharding + routing safety)."""

    ACTIVE = "active"
    READ_ONLY = "read_only"
    DRAINING = "draining"
    OFFLINE = "offline"

    @property
    def accepts_writes(self) -> bool:
        """True iff a write may be routed to a shard in this state."""
        return self is ShardState.ACTIVE

    @property
    def accepts_reads(self) -> bool:
        """True iff a read may be routed to a shard in this state."""
        return self in (ShardState.ACTIVE, ShardState.READ_ONLY, ShardState.DRAINING)


@dataclass(frozen=True, slots=True)
class Shard:
    """One physical shard: a logical id + connection coordinates + state.

    ``primary_url`` / ``replica_url`` are SQLAlchemy async URLs, the same shape
    :class:`~app.db.engine.EngineConfig` consumes — so a per-shard
    :class:`~app.db.engine.EngineRegistry` is one line away. ``weight`` biases
    the consistent-hashing ring (a bigger box owns proportionally more of the
    keyspace). ``zone`` is an optional placement hint (rack/AZ) for future
    locality-aware routing; it never affects correctness.
    """

    id: str
    primary_url: str
    replica_url: str | None = None
    state: ShardState = ShardState.ACTIVE
    weight: int = 1
    zone: str | None = None
    #: Free-form labels (e.g. ``{"generation": "2"}``) for operator tooling.
    labels: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("Shard.id must be non-empty")
        if not self.primary_url:
            raise ValueError(f"shard {self.id!r}: primary_url must be non-empty")
        if self.weight < 1:
            raise ValueError(f"shard {self.id!r}: weight must be >= 1")

    @property
    def accepts_writes(self) -> bool:
        """True iff this shard's state currently accepts writes."""
        return self.state.accepts_writes

    @property
    def accepts_reads(self) -> bool:
        """True iff this shard's state currently accepts reads."""
        return self.state.accepts_reads

    def with_state(self, state: ShardState) -> Shard:
        """Return a copy in a new state (topology transitions are pure copies)."""
        return replace(self, state=state)


@dataclass(frozen=True, slots=True)
class ShardTopology:
    """An immutable, ordered set of shards keyed by id.

    Ordering is stable (insertion order is preserved and ids are unique) so any
    strategy that enumerates shards — modulo-hash bucket assignment, range bound
    listing, ring construction — is reproducible across processes. Mutating
    operations (:meth:`with_shard`, :meth:`without_shard`, :meth:`with_state`)
    return a new topology; the original is never changed, which makes a
    resharding plan a clean before/after pair.
    """

    shards: tuple[Shard, ...]

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for shard in self.shards:
            if shard.id in seen:
                raise ValueError(f"duplicate shard id in topology: {shard.id!r}")
            seen.add(shard.id)

    # -- constructors -------------------------------------------------------- #

    @classmethod
    def of(cls, *shards: Shard) -> ShardTopology:
        """Build a topology from positional shards."""
        return cls(shards=tuple(shards))

    @classmethod
    def from_iterable(cls, shards: Iterable[Shard]) -> ShardTopology:
        """Build a topology from any iterable of shards."""
        return cls(shards=tuple(shards))

    # -- lookups ------------------------------------------------------------- #

    def get(self, shard_id: str) -> Shard:
        """Return the shard with ``shard_id`` (raises :class:`KeyError` if absent)."""
        for shard in self.shards:
            if shard.id == shard_id:
                return shard
        raise KeyError(f"no shard with id {shard_id!r}")

    def has(self, shard_id: str) -> bool:
        """True iff a shard with this id exists in the topology."""
        return any(s.id == shard_id for s in self.shards)

    @property
    def ids(self) -> tuple[str, ...]:
        """All shard ids in stable order."""
        return tuple(s.id for s in self.shards)

    def active(self) -> tuple[Shard, ...]:
        """Shards currently in the ``ACTIVE`` state."""
        return tuple(s for s in self.shards if s.state is ShardState.ACTIVE)

    def writable(self) -> tuple[Shard, ...]:
        """Shards that currently accept writes."""
        return tuple(s for s in self.shards if s.accepts_writes)

    def readable(self) -> tuple[Shard, ...]:
        """Shards that currently accept reads."""
        return tuple(s for s in self.shards if s.accepts_reads)

    def __len__(self) -> int:
        return len(self.shards)

    def __iter__(self) -> Iterator[Shard]:
        return iter(self.shards)

    def __contains__(self, shard_id: object) -> bool:
        return isinstance(shard_id, str) and self.has(shard_id)

    # -- transitions (pure) -------------------------------------------------- #

    def with_shard(self, shard: Shard) -> ShardTopology:
        """Add a new shard (raises if the id already exists)."""
        if self.has(shard.id):
            raise ValueError(f"shard {shard.id!r} already in topology")
        return ShardTopology(shards=(*self.shards, shard))

    def replace_shard(self, shard: Shard) -> ShardTopology:
        """Replace an existing shard by id (raises if absent)."""
        if not self.has(shard.id):
            raise KeyError(f"no shard with id {shard.id!r}")
        return ShardTopology(
            shards=tuple(shard if s.id == shard.id else s for s in self.shards)
        )

    def without_shard(self, shard_id: str) -> ShardTopology:
        """Remove a shard by id (raises if absent)."""
        if not self.has(shard_id):
            raise KeyError(f"no shard with id {shard_id!r}")
        return ShardTopology(shards=tuple(s for s in self.shards if s.id != shard_id))

    def with_state(self, shard_id: str, state: ShardState) -> ShardTopology:
        """Transition one shard to a new state, returning a new topology."""
        return self.replace_shard(self.get(shard_id).with_state(state))


__all__ = [
    "Shard",
    "ShardState",
    "ShardTopology",
]
