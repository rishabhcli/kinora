"""Rebalance planner: turn a topology change into an ordered set of moves.

A single :class:`~app.datascale.sharding.resharding.ReshardingJob` moves one key
set between two shards. *Rebalancing* is the layer above it: an operator adds or
removes a shard, and the planner computes **which** keys/slots must move **where**
to restore balance, with a movement estimate so the operator can size the
maintenance window before anything runs.

Two flavours, matching the two resharding models:

* **Slot rebalance** (the clean one). Diff the old and new
  :class:`~app.datascale.sharding.slots.SlotMap`; each changed ``(source,
  target)`` slot group becomes a :class:`SlotMoveOp`. The data to move is exactly
  the rows in those slots — exact, auditable, minimal.

* **Consistent-hash rebalance** (for the ring strategy). Adding/removing a shard
  changes which ring arc each key falls in; the planner samples the keyspace (or
  takes a key inventory) to estimate the fraction that moves and to which shard,
  producing :class:`RingMoveEstimate`\\ s. (Consistent hashing already minimises
  this — ~1/N — which is the whole point of using it.)

The planner is pure: it emits a :class:`RebalancePlan` describing the work and an
estimated cost; *executing* it is a sequence of resharding jobs the caller drives
(one per move), so each move keeps the online dual-write / cutover guarantees.
Emitting the plan without running it is the safety valve.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.datascale.sharding.keys import ShardKey
from app.datascale.sharding.slots import SlotMap, SlotMigration, migration_set
from app.datascale.sharding.strategy import ConsistentHashStrategy
from app.datascale.sharding.topology import ShardTopology


@dataclass(frozen=True, slots=True)
class SlotMoveOp:
    """One slot-range move in a slot rebalance (source → target, N slots)."""

    source: str
    target: str
    slots: tuple[int, ...]

    @property
    def slot_count(self) -> int:
        return len(self.slots)


@dataclass(frozen=True, slots=True)
class RingMoveEstimate:
    """An estimated key move in a consistent-hash rebalance."""

    source: str
    target: str
    estimated_keys: int


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    """The full set of moves to rebalance, plus a movement estimate.

    Either ``slot_moves`` (slot model) or ``ring_moves`` (ring model) is
    populated. ``total_units_moved`` is slots or keys respectively — the headline
    number an operator reads to size the window. :meth:`is_noop` is True when the
    topology change required no movement (e.g. removing an already-empty shard).
    """

    slot_moves: tuple[SlotMoveOp, ...] = ()
    ring_moves: tuple[RingMoveEstimate, ...] = ()
    total_units_moved: int = 0
    note: str = ""

    @property
    def is_noop(self) -> bool:
        return self.total_units_moved == 0

    def per_shard_inflow(self) -> dict[str, int]:
        """Units flowing *into* each target shard (capacity-planning view)."""
        inflow: dict[str, int] = {}
        for op in self.slot_moves:
            inflow[op.target] = inflow.get(op.target, 0) + op.slot_count
        for est in self.ring_moves:
            inflow[est.target] = inflow.get(est.target, 0) + est.estimated_keys
        return inflow

    def per_shard_outflow(self) -> dict[str, int]:
        """Units flowing *out of* each source shard."""
        outflow: dict[str, int] = {}
        for op in self.slot_moves:
            outflow[op.source] = outflow.get(op.source, 0) + op.slot_count
        for est in self.ring_moves:
            outflow[est.source] = outflow.get(est.source, 0) + est.estimated_keys
        return outflow

    def explain(self) -> str:
        """A human-readable summary (for the admin CLI / logs)."""
        lines = [f"RebalancePlan total_units={self.total_units_moved}"]
        if self.note:
            lines.append(f"  note: {self.note}")
        for op in self.slot_moves:
            lines.append(f"  slots {op.slot_count}: {op.source} -> {op.target}")
        for est in self.ring_moves:
            lines.append(f"  ~{est.estimated_keys} keys: {est.source} -> {est.target}")
        return "\n".join(lines)


def plan_slot_rebalance(before: SlotMap, after: SlotMap) -> RebalancePlan:
    """Plan the slot moves to go from ``before`` to ``after`` (exact)."""
    migrations: Sequence[SlotMigration] = migration_set(before, after)
    ops = tuple(
        SlotMoveOp(source=m.source, target=m.target, slots=m.slots) for m in migrations
    )
    total = sum(op.slot_count for op in ops)
    return RebalancePlan(
        slot_moves=ops,
        total_units_moved=total,
        note=f"slot rebalance over {before.slot_count} slots",
    )


def plan_add_shard_slots(current: SlotMap, new_shard_id: str) -> tuple[RebalancePlan, SlotMap]:
    """Plan adding ``new_shard_id`` to a slot map; return (plan, new map)."""
    after = current.with_shard_added(new_shard_id)
    return plan_slot_rebalance(current, after), after


def plan_remove_shard_slots(current: SlotMap, shard_id: str) -> tuple[RebalancePlan, SlotMap]:
    """Plan removing ``shard_id`` from a slot map; return (plan, new map)."""
    after = current.with_shard_removed(shard_id)
    return plan_slot_rebalance(current, after), after


def plan_ring_rebalance(
    before: ConsistentHashStrategy,
    after: ConsistentHashStrategy,
    *,
    sample_keys: Sequence[ShardKey],
) -> RebalancePlan:
    """Estimate the key moves for a consistent-hash topology change.

    The ring already minimises movement; this quantifies it. We route each
    sample key under both rings and group the keys whose owner changed by
    ``(source, target)``. The estimate scales with the sample: a representative
    sample of the real keyspace gives a movement fraction the operator can
    multiply by the true row count.
    """
    grouped: dict[tuple[str, str], int] = {}
    for key in sample_keys:
        src = before.route_one(key)
        tgt = after.route_one(key)
        if src != tgt:
            grouped[(src, tgt)] = grouped.get((src, tgt), 0) + 1
    moves = tuple(
        RingMoveEstimate(source=src, target=tgt, estimated_keys=count)
        for (src, tgt), count in grouped.items()
    )
    total = sum(m.estimated_keys for m in moves)
    fraction = (total / len(sample_keys)) if sample_keys else 0.0
    return RebalancePlan(
        ring_moves=moves,
        total_units_moved=total,
        note=f"consistent-hash rebalance; sampled {len(sample_keys)} keys, "
        f"moved fraction ≈ {fraction:.3f}",
    )


def topology_delta(
    before: ShardTopology, after: ShardTopology
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(added_ids, removed_ids)`` between two topologies."""
    before_ids = set(before.ids)
    after_ids = set(after.ids)
    added = tuple(sid for sid in after.ids if sid not in before_ids)
    removed = tuple(sid for sid in before.ids if sid not in after_ids)
    return added, removed


__all__ = [
    "RebalancePlan",
    "RingMoveEstimate",
    "SlotMoveOp",
    "plan_add_shard_slots",
    "plan_remove_shard_slots",
    "plan_ring_rebalance",
    "plan_slot_rebalance",
    "topology_delta",
]
