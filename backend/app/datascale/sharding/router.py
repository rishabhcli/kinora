"""Shard router: resolve a query to its target shard(s), state-aware.

A :class:`ShardStrategy` answers the *logical* question — where does a key live.
The :class:`ShardRouter` layers the *operational* questions on top:

* **Access intent.** A write may only land on a shard that ``accepts_writes``; a
  read may land on any shard that ``accepts_reads``. During a reshard a source
  shard goes ``READ_ONLY`` then ``DRAINING``; the router refuses to route a new
  write there and surfaces a clear :class:`RoutingError` rather than letting the
  write hit a server that will reject or, worse, silently diverge it.

* **Single vs scatter.** A query carrying a concrete shard key routes to one
  shard (the fast path — the per-book read in `§8` never fans out). A query with
  *no* key, or a key *range*, is a scatter: the router returns the set of shards
  the planner must fan out to.

* **Resharding overlap.** While a key is migrating (dual-write window) it has two
  homes — the old shard and the new one. The router consults an optional
  :class:`MigrationOverlay` so a write fans out to *both* homes and a read picks
  the authoritative one, all without the strategy itself knowing a reshard is in
  flight. This is the seam :mod:`app.datascale.sharding.resharding` drives.

The router is pure and synchronous: it computes *where*, never *connects*. The
connection proxy turns a resolved shard id into a live session.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.datascale.sharding.keys import ShardKey, ShardKeyValue, coerce_key
from app.datascale.sharding.strategy import RoutingError, ShardStrategy
from app.datascale.sharding.topology import ShardTopology


class Access(enum.Enum):
    """The access intent of a routed operation."""

    READ = "read"
    WRITE = "write"

    @property
    def is_write(self) -> bool:
        return self is Access.WRITE


@dataclass(frozen=True, slots=True)
class MigrationOverlay:
    """In-flight key→shard moves layered over the base strategy.

    During the dual-write window of an online move/split, a key's authoritative
    home is changing. The overlay records, per moving key, the ``source`` shard
    (still authoritative for reads until cutover) and the ``target`` shard
    (receiving dual writes). After cutover the entry flips to ``cutover=True`` and
    the target becomes authoritative. :mod:`app.datascale.sharding.resharding`
    owns the lifecycle; the router only reads this map.
    """

    #: ``key → (source_shard, target_shard, cutover)``.
    moves: Mapping[ShardKey, tuple[str, str, bool]] = field(default_factory=dict)

    def write_targets(self, key: ShardKey, base: str) -> tuple[str, ...]:
        """Shards a *write* for ``key`` must hit (both homes during dual-write)."""
        entry = self.moves.get(key)
        if entry is None:
            return (base,)
        source, target, cutover = entry
        if cutover:
            return (target,)
        # Dual-write window: write both homes so neither diverges.
        return (source, target) if source != target else (source,)

    def read_target(self, key: ShardKey, base: str) -> str:
        """The single authoritative shard a *read* for ``key`` should use."""
        entry = self.moves.get(key)
        if entry is None:
            return base
        source, target, cutover = entry
        return target if cutover else source

    def is_empty(self) -> bool:
        return not self.moves


@dataclass(frozen=True, slots=True)
class Resolution:
    """The router's answer: the shard(s) an operation targets.

    ``scatter`` is True when more than one shard is involved (a fan-out the
    planner/executor must coordinate). ``key`` is the routed key when single-key,
    else ``None`` (a keyless/range scatter).
    """

    shard_ids: tuple[str, ...]
    access: Access
    scatter: bool
    key: ShardKey | None = None

    @property
    def single(self) -> str:
        """The lone target shard id (raises if this was a scatter)."""
        if len(self.shard_ids) != 1:
            raise RoutingError(
                f"expected a single-shard resolution, got {len(self.shard_ids)} shards"
            )
        return self.shard_ids[0]


@dataclass(frozen=True, slots=True)
class ShardRouter:
    """Resolve queries to shards over a strategy + topology, with state safety.

    The router does not own the strategy's placement logic; it composes it with
    the topology's *state* and an optional :class:`MigrationOverlay`. Build one
    per logical table family (everything sharded by the same key) — e.g. one
    router for the book-keyed tables, another for the user-keyed tables.
    """

    strategy: ShardStrategy
    topology: ShardTopology
    overlay: MigrationOverlay = field(default_factory=MigrationOverlay)

    # -- single-key routing -------------------------------------------------- #

    def route(
        self,
        key: ShardKey | ShardKeyValue | Mapping[str, ShardKeyValue],
        *,
        access: Access = Access.READ,
    ) -> Resolution:
        """Resolve a single-key operation to its target shard(s).

        A read resolves to exactly one shard (the authoritative home). A write
        resolves to one shard normally, or *both* homes when the key is mid-move
        (dual-write). Every target is checked against shard state for the access;
        an unroutable target raises :class:`RoutingError`.
        """
        shard_key = coerce_key(key)
        base = self.strategy.route_one(shard_key)
        if access.is_write:
            targets = self.overlay.write_targets(shard_key, base)
        else:
            targets = (self.overlay.read_target(shard_key, base),)
        self._assert_routable(targets, access)
        return Resolution(
            shard_ids=tuple(targets),
            access=access,
            scatter=len(targets) > 1,
            key=shard_key,
        )

    def route_to_shard_id(
        self,
        key: ShardKey | ShardKeyValue | Mapping[str, ShardKeyValue],
        *,
        access: Access = Access.READ,
    ) -> str:
        """Convenience: resolve a single-key *read* to one shard id."""
        return self.route(key, access=access).single

    # -- range / scatter routing -------------------------------------------- #

    def route_range(
        self,
        low: ShardKeyValue | None,
        high: ShardKeyValue | None,
        *,
        access: Access = Access.READ,
    ) -> Resolution:
        """Resolve a half-open key range ``[low, high)`` to the shards it spans.

        Range strategies prune to the touched shards; hash/directory strategies
        scatter to all shards (a range query has no locality there). Either way
        the result is a scatter the planner coordinates.
        """
        targets = self._filter_for_access(self.strategy.route_range(low, high), access)
        return Resolution(
            shard_ids=tuple(targets),
            access=access,
            scatter=len(targets) != 1,
        )

    def scatter_all(self, *, access: Access = Access.READ) -> Resolution:
        """Resolve a keyless query to *every* routable shard (a full fan-out).

        Used by aggregate/listing queries with no shard-key predicate. Targets
        not routable for the access (e.g. OFFLINE on a read) are excluded — a
        scatter is allowed to proceed against the reachable subset and the
        executor records the skipped shards as partial-failure context.
        """
        targets = self._filter_for_access(self.strategy.all_shards(), access)
        return Resolution(
            shard_ids=tuple(targets),
            access=access,
            scatter=len(targets) != 1,
        )

    # -- helpers ------------------------------------------------------------- #

    def _assert_routable(self, shard_ids: Iterable[str], access: Access) -> None:
        """Raise unless *every* target accepts the access in its current state.

        Single-key routing is strict: if the one home a write must hit is not
        writable, failing loud is correct — silently dropping or rerouting a
        write would corrupt the shard assignment.
        """
        for sid in shard_ids:
            shard = self.topology.get(sid)
            ok = shard.accepts_writes if access.is_write else shard.accepts_reads
            if not ok:
                raise RoutingError(
                    f"shard {sid!r} (state={shard.state.value}) does not accept "
                    f"{access.value}s"
                )

    def _filter_for_access(self, shard_ids: Iterable[str], access: Access) -> list[str]:
        """Keep only targets that accept the access; preserve order, dedup."""
        out: list[str] = []
        seen: set[str] = set()
        for sid in shard_ids:
            if sid in seen:
                continue
            seen.add(sid)
            shard = self.topology.get(sid)
            ok = shard.accepts_writes if access.is_write else shard.accepts_reads
            if ok:
                out.append(sid)
        return out

    def with_overlay(self, overlay: MigrationOverlay) -> ShardRouter:
        """Return a copy bound to a new migration overlay (resharding seam)."""
        return ShardRouter(strategy=self.strategy, topology=self.topology, overlay=overlay)


__all__ = [
    "Access",
    "MigrationOverlay",
    "Resolution",
    "ShardRouter",
]
