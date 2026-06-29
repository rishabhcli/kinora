"""The replication log: per-node ordered streams of versioned updates.

Active-active replication ships *operations*, not whole-state snapshots. Every
local write a node accepts becomes a :class:`ReplicationRecord` appended to that
node's segment of the log with a monotonically increasing per-node sequence
number. Peers pull records by ``(origin_node, after_seq)`` and apply them in
sequence — the async-log-shipping half of the protocol.

* :class:`WriteOp` — the payload: set/delete a key to a value at a timestamp.
* :class:`ReplicationRecord` — a write plus its origin, sequence, and the
  causal context (the writer's version vector at the time) needed for
  causal-delivery ordering.
* :class:`ReplicationLog` — an in-memory, append-only multi-segment log with the
  cursor reads anti-entropy needs. Durable in the sense that records, once
  appended, are immutable and gap-free per node; the storage backend is an
  injection seam (this pure version keeps lists in memory for the simulator).

Pure and deterministic. No real storage; persistence is left to an adapter.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from app.distributed.replication.clock import HybridTimestamp, NodeId
from app.distributed.replication.version import VersionVector


class OpKind(StrEnum):
    """The two kinds of write the protocol ships."""

    SET = "set"
    DELETE = "delete"


@dataclass(frozen=True, slots=True)
class WriteOp:
    """A single key mutation. ``value`` is ignored for :attr:`OpKind.DELETE`.

    The ``value`` is whatever the bound conflict resolver understands — a
    :class:`~app.distributed.replication.conflict.Stamped` scalar, a CRDT value,
    or an app object. The log is value-agnostic; resolution happens at apply.
    """

    key: str
    kind: OpKind
    value: Any = None

    @classmethod
    def set(cls, key: str, value: Any) -> WriteOp:
        return cls(key, OpKind.SET, value)

    @classmethod
    def delete(cls, key: str) -> WriteOp:
        return cls(key, OpKind.DELETE, None)


@dataclass(frozen=True, slots=True)
class ReplicationRecord:
    """One durable, immutable log entry.

    ``origin`` + ``seq`` is the record's globally unique identity and its
    per-origin total order. ``timestamp`` is the HLC stamp used for conflict
    resolution. ``deps`` is the writer's version vector *excluding* this record —
    the causal context a receiver checks before applying, so effects never land
    before their causes.
    """

    origin: NodeId
    seq: int
    timestamp: HybridTimestamp
    op: WriteOp
    deps: VersionVector = field(default_factory=VersionVector.empty)

    @property
    def key(self) -> str:
        return self.op.key

    def causally_ready(self, applied: VersionVector) -> bool:
        """True iff a node whose frontier is ``applied`` may apply this record now.

        Requires (a) every dependency satisfied and (b) this is the *next*
        record from ``origin`` (no gap), which together guarantee causal +
        per-origin order.
        """
        return applied.dominates(self.deps) and applied.get(self.origin) == self.seq - 1


class SequenceGapError(RuntimeError):
    """Raised when appending a record that is not the next in its origin segment."""


class ReplicationLog:
    """An append-only, per-origin-ordered log of replication records.

    Each origin node owns a *segment*: a gap-free sequence ``1, 2, 3, …``. The
    log enforces that invariant on append, so a segment never has holes and a
    cursor read ``records_after(origin, n)`` returns a contiguous suffix.
    """

    def __init__(self) -> None:
        self._segments: dict[NodeId, list[ReplicationRecord]] = {}

    def append(self, record: ReplicationRecord) -> None:
        """Append ``record``; it must be exactly ``next_seq`` for its origin."""
        segment = self._segments.setdefault(record.origin, [])
        expected = len(segment) + 1
        if record.seq != expected:
            raise SequenceGapError(
                f"{record.origin} expected seq {expected}, got {record.seq}"
            )
        self._segments[record.origin] = [*segment, record]

    def next_seq(self, origin: NodeId) -> int:
        """The sequence number the next append from ``origin`` must carry."""
        return len(self._segments.get(origin, [])) + 1

    def high_water(self) -> VersionVector:
        """The frontier covering every record currently in the log."""
        return VersionVector.of({n: len(s) for n, s in self._segments.items()})

    def records_after(self, origin: NodeId, after_seq: int) -> Sequence[ReplicationRecord]:
        """The contiguous suffix of ``origin``'s segment with ``seq > after_seq``."""
        segment = self._segments.get(origin, [])
        return tuple(r for r in segment if r.seq > after_seq)

    def delta_since(self, frontier: VersionVector) -> list[ReplicationRecord]:
        """Every record the holder of ``frontier`` has not yet seen, in causal-safe order.

        Records are returned sorted by their HLC :attr:`timestamp` so a receiver
        applying them in order satisfies causality (a cause's stamp always
        precedes its effect's). Within the same timestamp the node tiebreak makes
        the order deterministic.
        """
        out: list[ReplicationRecord] = []
        for origin, segment in self._segments.items():
            cut = frontier.get(origin)
            out.extend(r for r in segment if r.seq > cut)
        out.sort(key=lambda r: r.timestamp)
        return out

    def all_records(self) -> Iterator[ReplicationRecord]:
        for segment in self._segments.values():
            yield from segment

    def segment(self, origin: NodeId) -> Sequence[ReplicationRecord]:
        return tuple(self._segments.get(origin, []))

    def __len__(self) -> int:
        return sum(len(s) for s in self._segments.values())


def truncate_record(record: ReplicationRecord, max_value_repr: int = 256) -> ReplicationRecord:
    """Return a record with an oversize string value clipped (for logging only)."""
    if isinstance(record.op.value, str) and len(record.op.value) > max_value_repr:
        clipped = record.op.value[:max_value_repr] + "…"
        return replace(record, op=replace(record.op, value=clipped))
    return record


def merge_logs(logs: Iterable[ReplicationLog]) -> ReplicationLog:
    """Build one log holding the union of records across ``logs`` (idempotent).

    Used by the simulator to assemble a god's-eye log. Identical records (same
    origin+seq) collapse; conflicting records at the same origin+seq are a bug
    and raise via the append invariant.
    """
    merged = ReplicationLog()
    seen: dict[tuple[NodeId, int], ReplicationRecord] = {}
    for log in logs:
        for record in log.all_records():
            key = (record.origin, record.seq)
            existing = seen.get(key)
            if existing is None:
                seen[key] = record
            elif existing != record:
                raise SequenceGapError(
                    f"conflicting records at {record.origin} seq {record.seq}"
                )
    for record in sorted(seen.values(), key=lambda r: (str(r.origin), r.seq)):
        merged.append(record)
    return merged
