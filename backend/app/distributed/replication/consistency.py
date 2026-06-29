"""Tunable consistency: ONE / QUORUM / ALL + bounded-staleness reads.

Active-active replication is eventually consistent by default, but a caller
often wants a stronger per-operation guarantee. This module is the pure decision
logic for that, parameterised by the replica set:

* :class:`ConsistencyLevel` — ONE, QUORUM, ALL: how many replica acks a write
  must collect (or how many replicas a read must agree) before it counts as
  satisfied. QUORUM uses the strict-majority rule ``floor(N/2)+1`` so a
  read-quorum and a write-quorum always intersect (``R + W > N``), the classic
  guarantee that a QUORUM read sees the latest QUORUM write.
* :class:`WriteCoordinator` — collects :class:`~...node.WriteReceipt`-style acks
  for an in-flight write and reports when its level is met.
* :class:`ReadCoordinator` — gathers per-replica answers for a key and resolves
  the value the level requires (newest among a quorum), surfacing staleness.
* :class:`StalenessPolicy` — bounded-staleness: a read result carries the *age*
  of the data (now minus the freshest contributing write's wall clock); the
  policy decides whether that age is within the caller's tolerance.

All pure: no transport, no clock side effects. The coordinator is fed acks/answers
by the caller (or the simulator) and makes the decision; it does not do I/O.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, TypeVar

from app.distributed.replication.clock import HybridTimestamp, NodeId

T = TypeVar("T")


class ConsistencyLevel(Enum):
    """How many replicas must participate for an operation to be satisfied."""

    ONE = "one"
    QUORUM = "quorum"
    ALL = "all"

    def required(self, replica_count: int) -> int:
        """The number of replica acks/answers this level needs over ``replica_count``."""
        if replica_count <= 0:
            raise ValueError("replica_count must be positive")
        if self is ConsistencyLevel.ONE:
            return 1
        if self is ConsistencyLevel.ALL:
            return replica_count
        return replica_count // 2 + 1  # strict majority


def quorum_overlaps(read: ConsistencyLevel, write: ConsistencyLevel, replicas: int) -> bool:
    """True iff ``R + W > N`` — a read at ``read`` is guaranteed to see a write at ``write``.

    The linchpin of tunable consistency: QUORUM/QUORUM, ONE/ALL, and ALL/ONE all
    overlap; ONE/ONE and ONE/QUORUM do not (a read may miss the freshest write).
    """
    return read.required(replicas) + write.required(replicas) > replicas


@dataclass(frozen=True, slots=True)
class WriteAck:
    """A replica's acknowledgement that it durably holds a specific write."""

    replica: NodeId
    timestamp: HybridTimestamp


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """The result of coordinating a write at a consistency level."""

    satisfied: bool
    acks: int
    required: int
    acked_by: frozenset[NodeId]


class WriteCoordinator:
    """Collects acks for one in-flight write and reports when the level is met."""

    def __init__(self, level: ConsistencyLevel, replicas: Sequence[NodeId]) -> None:
        if not replicas:
            raise ValueError("a write needs at least one replica")
        self._level = level
        self._replicas = tuple(replicas)
        self._required = level.required(len(self._replicas))
        self._acked: set[NodeId] = set()

    @property
    def required(self) -> int:
        return self._required

    def ack(self, replica: NodeId) -> None:
        if replica in self._replicas:
            self._acked.add(replica)

    @property
    def satisfied(self) -> bool:
        return len(self._acked) >= self._required

    def outcome(self) -> WriteOutcome:
        return WriteOutcome(
            satisfied=self.satisfied,
            acks=len(self._acked),
            required=self._required,
            acked_by=frozenset(self._acked),
        )


@dataclass(frozen=True, slots=True)
class ReplicaAnswer(Generic[T]):
    """One replica's answer to a read: a value (or absent) and its write stamp."""

    replica: NodeId
    value: T | None
    timestamp: HybridTimestamp | None
    present: bool = True


@dataclass(frozen=True, slots=True)
class ReadResult(Generic[T]):
    """The resolved read plus the metadata bounded-staleness needs."""

    value: T | None
    timestamp: HybridTimestamp | None
    satisfied: bool
    answers: int
    required: int
    present: bool


class ReadCoordinator(Generic[T]):
    """Gathers per-replica answers and resolves the value the level requires.

    Resolution is "newest wins among the answers collected" — the answer with
    the highest :class:`HybridTimestamp`. With a quorum that intersects the write
    quorum, that newest answer is provably the latest committed write.
    """

    def __init__(self, level: ConsistencyLevel, replicas: Sequence[NodeId]) -> None:
        if not replicas:
            raise ValueError("a read needs at least one replica")
        self._level = level
        self._replicas = tuple(replicas)
        self._required = level.required(len(self._replicas))
        self._answers: dict[NodeId, ReplicaAnswer[T]] = {}

    @property
    def required(self) -> int:
        return self._required

    def answer(self, ans: ReplicaAnswer[T]) -> None:
        if ans.replica in self._replicas:
            self._answers[ans.replica] = ans

    @property
    def satisfied(self) -> bool:
        return len(self._answers) >= self._required

    def result(self) -> ReadResult[T]:
        present_answers = [a for a in self._answers.values() if a.present and a.timestamp]
        if not present_answers:
            return ReadResult(
                value=None,
                timestamp=None,
                satisfied=self.satisfied,
                answers=len(self._answers),
                required=self._required,
                present=False,
            )
        newest = max(present_answers, key=lambda a: a.timestamp)  # type: ignore[arg-type,return-value]
        return ReadResult(
            value=newest.value,
            timestamp=newest.timestamp,
            satisfied=self.satisfied,
            answers=len(self._answers),
            required=self._required,
            present=True,
        )


@dataclass(frozen=True, slots=True)
class StalenessPolicy:
    """A bounded-staleness tolerance, expressed as a max data age in ms."""

    max_age_ms: int

    def age_of(self, result: ReadResult[Any], now_ms: int) -> int | None:
        """Age (ms) of ``result`` at ``now_ms`` — now minus the write's wall clock."""
        if result.timestamp is None:
            return None
        return max(0, now_ms - result.timestamp.wall_ms)

    def within_bound(self, result: ReadResult[Any], now_ms: int) -> bool:
        """True iff the read is fresh enough (or absent, which has no age)."""
        age = self.age_of(result, now_ms)
        if age is None:
            return True
        return age <= self.max_age_ms


def freshest(answers: Mapping[NodeId, HybridTimestamp]) -> NodeId | None:
    """The replica holding the newest write among ``answers`` (None if empty)."""
    if not answers:
        return None
    return max(answers, key=lambda n: answers[n])
