"""Fixed hash-slot sharding (Redis-Cluster-shaped) — resharding by reassignment.

The four strategies in :mod:`~app.datascale.sharding.strategy` map a key
*directly* to a shard. That makes resharding a per-key data move. A *slot map*
adds one level of indirection that turns resharding into cheap **metadata**:

    key  ──hash──▶  slot (one of a fixed N, e.g. 16384)  ──map──▶  shard

The slot count is fixed for the life of the cluster, so a key's *slot* never
changes; only the *slot → shard* assignment moves. Rebalancing is therefore
"reassign these slots from shard A to shard B" — a small, exact, auditable set of
ranges — and the data to migrate is precisely the rows in those slots. This is
the model Redis Cluster and Citus use, and it is the most operationally pleasant
one at scale.

:class:`SlotMap` owns the assignment (and is an immutable value, so a rebalance
is a clean before/after pair). :class:`SlotStrategy` adapts it to the
:class:`~app.datascale.sharding.strategy.ShardStrategy` interface so the router,
planner and resharding machinery all work over slots unchanged. Helpers compute
a *balanced* assignment and the migration set between two assignments.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from app.datascale.sharding.keys import ShardKey, ShardKeyValue
from app.datascale.sharding.strategy import ShardStrategy, StrategyKind
from app.datascale.sharding.topology import ShardTopology

#: The conventional fixed slot count (Redis Cluster uses 16384). Any positive
#: count works; larger ⇒ finer rebalancing granularity at a bigger map.
DEFAULT_SLOT_COUNT = 16384


@dataclass(frozen=True, slots=True)
class SlotMap:
    """An immutable assignment of each of ``slot_count`` slots to a shard id.

    ``assignment`` is a tuple indexed by slot number; ``assignment[s]`` is the
    shard owning slot ``s``. Built balanced via :meth:`balanced`, then rebalanced
    by :meth:`reassign` / :meth:`with_shard_added` / :meth:`with_shard_removed`,
    each returning a new map.
    """

    slot_count: int
    assignment: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.slot_count < 1:
            raise ValueError("slot_count must be >= 1")
        if len(self.assignment) != self.slot_count:
            raise ValueError(
                f"assignment length {len(self.assignment)} != slot_count {self.slot_count}"
            )

    @classmethod
    def balanced(
        cls, shard_ids: Sequence[str], *, slot_count: int = DEFAULT_SLOT_COUNT
    ) -> SlotMap:
        """Assign slots as evenly as possible across ``shard_ids`` (contiguous runs).

        Contiguous runs (slot 0..k → shard0, k+1..2k → shard1, …) keep each
        shard's slots a small number of ranges, which makes the migration set
        compact. The remainder is spread one-slot-each across the first shards so
        the imbalance is at most one slot.
        """
        if not shard_ids:
            raise ValueError("balanced() needs at least one shard")
        n = len(shard_ids)
        base, extra = divmod(slot_count, n)
        assignment: list[str] = []
        for i, sid in enumerate(shard_ids):
            count = base + (1 if i < extra else 0)
            assignment.extend([sid] * count)
        return cls(slot_count=slot_count, assignment=tuple(assignment))

    def shard_for_slot(self, slot: int) -> str:
        """The shard owning ``slot``."""
        if not 0 <= slot < self.slot_count:
            raise ValueError(f"slot {slot} out of range [0, {self.slot_count})")
        return self.assignment[slot]

    def slots_for_shard(self, shard_id: str) -> tuple[int, ...]:
        """Every slot currently owned by ``shard_id`` (ascending)."""
        return tuple(s for s, owner in enumerate(self.assignment) if owner == shard_id)

    def shard_ids(self) -> tuple[str, ...]:
        """Distinct shard ids that own at least one slot (stable order)."""
        out: list[str] = []
        seen: set[str] = set()
        for owner in self.assignment:
            if owner not in seen:
                seen.add(owner)
                out.append(owner)
        return tuple(out)

    def distribution(self) -> dict[str, int]:
        """Slot count per shard (balance diagnostics)."""
        counts: dict[str, int] = {}
        for owner in self.assignment:
            counts[owner] = counts.get(owner, 0) + 1
        return counts

    def reassign(self, slots: Mapping[int, str]) -> SlotMap:
        """Return a new map with the given ``{slot: new_shard}`` overrides applied."""
        new = list(self.assignment)
        for slot, shard_id in slots.items():
            if not 0 <= slot < self.slot_count:
                raise ValueError(f"slot {slot} out of range")
            new[slot] = shard_id
        return SlotMap(slot_count=self.slot_count, assignment=tuple(new))

    def with_shard_added(self, shard_id: str) -> SlotMap:
        """Rebalance to include ``shard_id``, moving the *fewest* slots possible.

        Steals an even share of slots from the currently most-loaded shards so
        the new shard reaches the fair share (``slot_count / new_shard_count``).
        Only the stolen slots move — minimal disruption.
        """
        if shard_id in self.shard_ids():
            raise ValueError(f"shard {shard_id!r} already owns slots")
        owners = [*self.shard_ids(), shard_id]
        target_each = self.slot_count // len(owners)
        new = list(self.assignment)
        moved = 0
        # Take from the most-loaded shards first for an even result.
        by_load = sorted(self.distribution().items(), key=lambda kv: kv[1], reverse=True)
        for donor, _count in by_load:
            if moved >= target_each:
                break
            donor_slots = [s for s, o in enumerate(new) if o == donor]
            # Leave the donor with at least target_each slots.
            steal = min(len(donor_slots) - target_each, target_each - moved)
            for s in donor_slots[:max(0, steal)]:
                new[s] = shard_id
                moved += 1
        return SlotMap(slot_count=self.slot_count, assignment=tuple(new))

    def with_shard_removed(self, shard_id: str) -> SlotMap:
        """Rebalance away from ``shard_id``, spreading its slots over the rest."""
        remaining = [s for s in self.shard_ids() if s != shard_id]
        if not remaining:
            raise ValueError("cannot remove the last shard")
        new = list(self.assignment)
        orphan = [s for s, o in enumerate(new) if o == shard_id]
        for i, slot in enumerate(orphan):
            new[slot] = remaining[i % len(remaining)]
        return SlotMap(slot_count=self.slot_count, assignment=tuple(new))


@dataclass(frozen=True, slots=True)
class SlotMigration:
    """The slots that move from ``source`` to ``target`` between two maps."""

    source: str
    target: str
    slots: tuple[int, ...]

    @property
    def count(self) -> int:
        return len(self.slots)


def migration_set(before: SlotMap, after: SlotMap) -> tuple[SlotMigration, ...]:
    """Compute the slot moves to go from ``before`` to ``after``.

    Groups changed slots by ``(source, target)`` so each migration is one shard
    pair — exactly the input a resharding job (or an operator) needs. Slots that
    did not change owner are omitted (no move). The two maps must share a slot
    count.
    """
    if before.slot_count != after.slot_count:
        raise ValueError("cannot diff slot maps with different slot counts")
    grouped: dict[tuple[str, str], list[int]] = {}
    for slot in range(before.slot_count):
        src = before.assignment[slot]
        tgt = after.assignment[slot]
        if src != tgt:
            grouped.setdefault((src, tgt), []).append(slot)
    return tuple(
        SlotMigration(source=src, target=tgt, slots=tuple(slots))
        for (src, tgt), slots in grouped.items()
    )


@dataclass(frozen=True, slots=True)
class SlotStrategy(ShardStrategy):
    """Adapt a :class:`SlotMap` to the :class:`ShardStrategy` interface.

    A key hashes to a slot (``hashed_mod(slot_count)``) and the slot map names the
    owning shard. The router/planner/executor are unchanged. Range queries over a
    slot strategy scatter to every shard the slot map currently uses (a hashed
    keyspace has no range locality), same as modulo-hash.
    """

    slot_map: SlotMap
    algo: str = "sha1"
    kind: StrategyKind = field(default=StrategyKind("slot"), init=False)

    def slot_for(self, key: ShardKey) -> int:
        """The slot a key hashes into."""
        return key.hashed_mod(self.slot_map.slot_count, algo=self.algo)

    def route_one(self, key: ShardKey) -> str:
        return self.slot_map.shard_for_slot(self.slot_for(key))

    def route_range(
        self, low: ShardKeyValue | None, high: ShardKeyValue | None
    ) -> tuple[str, ...]:
        return self.all_shards()

    def all_shards(self) -> tuple[str, ...]:
        return self.slot_map.shard_ids()

    def describe(self) -> Mapping[str, object]:
        return {
            "kind": self.kind.name,
            "slot_count": self.slot_map.slot_count,
            "distribution": self.slot_map.distribution(),
        }


def slot_map_covers_topology(slot_map: SlotMap, topology: ShardTopology) -> bool:
    """True iff every shard the slot map references exists in ``topology``."""
    return all(topology.has(sid) for sid in slot_map.shard_ids())


__all__ = [
    "DEFAULT_SLOT_COUNT",
    "SlotMap",
    "SlotMigration",
    "SlotStrategy",
    "migration_set",
    "slot_map_covers_topology",
]
