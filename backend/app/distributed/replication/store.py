"""The per-replica key-value state and the apply path.

A :class:`ReplicaStore` is one region's materialized view of the keyspace. It
holds the current converged value for each key plus the :class:`HybridTimestamp`
of the write that produced it, and applies incoming :class:`ReplicationRecord`
deterministically through the bound :class:`ConflictResolver`. Two stores that
have applied the same *set* of records — in any order — hold byte-identical
state: that is the convergence guarantee, and it is what the simulator proves.

Region affinity lives here too: each key may declare a *home region*
(:class:`KeyAffinity`). Affinity does not restrict who may write (active-active),
but it informs routing (write near home for lower commit latency) and is exposed
so the router can make placement decisions.

Pure and deterministic; no storage backend, no clock side effects.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.conflict import ResolverRegistry
from app.distributed.replication.log import OpKind, ReplicationRecord
from app.distributed.replication.version import VersionVector

#: A sentinel value marking a key whose latest write was a delete (a tombstone).
#: Kept (not removed) so a concurrent resurrection resolves against it correctly.
TOMBSTONE: Any = object()


@dataclass(frozen=True, slots=True)
class KeyAffinity:
    """Per-key region-placement metadata.

    ``home_region`` is the preferred region for low-latency writes; ``replicas``
    is the set of regions that must hold a copy (all, by default — full
    active-active). Affinity is advisory for correctness (every region converges
    regardless) but load-bearing for routing.
    """

    home_region: str
    replicas: frozenset[str] = frozenset()

    def is_replicated_in(self, region: str) -> bool:
        return not self.replicas or region in self.replicas or region == self.home_region


@dataclass(frozen=True, slots=True)
class Cell:
    """One key's converged value and the stamp of the write that set it."""

    value: Any
    timestamp: HybridTimestamp
    deleted: bool = False

    @property
    def is_present(self) -> bool:
        return not self.deleted


class ReplicaStore:
    """A single replica's converged keyspace plus its applied-frontier.

    Applying a record is idempotent and order-independent: if the record is
    already covered by :meth:`frontier` it is a no-op; otherwise the new value is
    merged with the current cell via the key's resolver and the frontier
    advances. The store never sees the network; the :class:`ReplicaNode` feeds it.
    """

    def __init__(self, node: NodeId, resolvers: ResolverRegistry) -> None:
        self._node = node
        self._resolvers = resolvers
        self._cells: dict[str, Cell] = {}
        self._affinity: dict[str, KeyAffinity] = {}
        self._frontier = VersionVector.empty()

    @property
    def node(self) -> NodeId:
        return self._node

    @property
    def region(self) -> str:
        return self._node.region

    def frontier(self) -> VersionVector:
        """The version vector of records this store has applied."""
        return self._frontier

    def set_affinity(self, key: str, affinity: KeyAffinity) -> None:
        self._affinity[key] = affinity

    def affinity(self, key: str) -> KeyAffinity | None:
        return self._affinity.get(key)

    def get(self, key: str) -> Any:
        """The current value, or ``None`` if absent/deleted."""
        cell = self._cells.get(key)
        if cell is None or cell.deleted:
            return None
        return cell.value

    def cell(self, key: str) -> Cell | None:
        return self._cells.get(key)

    def keys(self) -> frozenset[str]:
        return frozenset(k for k, c in self._cells.items() if not c.deleted)

    def items(self) -> Iterator[tuple[str, Any]]:
        for key, cell in self._cells.items():
            if not cell.deleted:
                yield key, cell.value

    def snapshot(self) -> Mapping[str, Cell]:
        """An immutable copy of every cell (including tombstones) for comparison."""
        return dict(self._cells)

    def apply(self, record: ReplicationRecord) -> bool:
        """Apply ``record`` deterministically. Returns ``True`` if state changed.

        Idempotent: a record already covered by the frontier is ignored. The new
        value is reconciled with the existing cell through the key's resolver
        (for SET) or marked deleted (for DELETE); the resolution itself decides
        the winner so the result is independent of arrival order.
        """
        if self._frontier.includes(record.origin, record.seq):
            return False
        key = record.key
        incoming = self._cell_for(record)
        current = self._cells.get(key)
        merged = incoming if current is None else self._merge_cells(key, current, incoming)
        self._cells[key] = merged
        self._frontier = self._frontier.advanced(record.origin, record.seq)
        return current != merged

    def merge_cell(self, key: str, incoming: Cell) -> bool:
        """Merge a foreign cell by value/timestamp *without* touching the frontier.

        This is the Merkle-repair path: anti-entropy reconstructs a converged
        cell from a peer's materialized store (its strict log tail may be gone)
        and folds it in. Because resolution is by value/timestamp it is
        idempotent and order-independent — re-applying or applying a stale cell
        is a no-op — so it converges without claiming a log sequence. Returns
        ``True`` if the cell changed.
        """
        current = self._cells.get(key)
        merged = incoming if current is None else self._merge_cells(key, current, incoming)
        if current == merged:
            return False
        self._cells[key] = merged
        return True

    def _cell_for(self, record: ReplicationRecord) -> Cell:
        if record.op.kind is OpKind.DELETE:
            return Cell(TOMBSTONE, record.timestamp, deleted=True)
        return Cell(record.op.value, record.timestamp)

    def _merge_cells(self, key: str, current: Cell, incoming: Cell) -> Cell:
        """Reconcile two cells for ``key`` using the bound resolver.

        Deletes are always resolved by timestamp (a delete is an LWW tombstone),
        so a delete vs delete or a delete vs present write picks the later cell.
        For two present writes the resolver mode decides:

        * timestamped (LWW family) — keep the higher-:class:`HybridTimestamp`
          cell directly; the value is opaque.
        * state-based (CRDT) — call ``resolve`` on the raw values to compute the
          joined value, keeping the higher timestamp for future tiebreaks.
        """
        resolver = self._resolvers.for_key(key)
        # Any delete involved -> pure LWW between the two cells.
        if current.deleted or incoming.deleted:
            return incoming if incoming.timestamp > current.timestamp else current
        if resolver.timestamped:
            return incoming if incoming.timestamp > current.timestamp else current
        merged_value = resolver.resolve(current.value, incoming.value)
        winning_ts = max(current.timestamp, incoming.timestamp)
        return Cell(merged_value, winning_ts)
