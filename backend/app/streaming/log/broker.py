"""The ``Broker`` protocol — the seam the whole streaming plane is built on.

A :class:`Broker` is the minimal async surface a partitioned-log implementation
must expose. The high-level :class:`~app.streaming.log.producer.Producer`,
:class:`~app.streaming.log.consumer.Consumer`, and the consumer-group
:class:`~app.streaming.log.group.coordinator.GroupCoordinator` are written
*against this protocol only* — so they run unchanged over the in-memory broker
(tests, ``app.streaming.log.memory``) and the Redis-Streams broker (production,
``app.streaming.log.redis``). The two sibling facets (processing, CDC) likewise
depend on this protocol, never on a concrete broker.

The surface is deliberately log-shaped, not queue-shaped:

* **Admin** — ``create_topic`` / ``delete_topic`` / ``topics`` / ``describe_topic``
  / ``partitions_for``.
* **Produce** — ``produce`` appends a (partition-resolved) record and returns its
  durable :class:`~app.streaming.log.record.RecordMetadata`. The
  *idempotence/transaction* logic lives in the producer; the broker enforces the
  per-producer sequence + epoch fence it is told about via ``ProduceContext``.
* **Consume** — ``fetch`` reads a window of records from one partition starting at
  an offset; ``end_offsets`` / ``beginning_offsets`` / ``offsets_for_times``
  bound seeks.
* **Group / offset store** — ``commit_offsets`` / ``fetch_committed`` /
  ``list_committed`` persist a group's progress; ``join_group`` / ``leave_group``
  / ``heartbeat`` / ``describe_group`` drive membership + rebalance. The
  in-memory and Redis brokers store this in the log's metadata; a real Kafka
  broker keeps it in ``__consumer_offsets``.

``ProduceContext`` carries the optional exactly-once metadata (producer id, epoch,
per-partition sequence, and whether the append is part of an open transaction).
A broker that doesn't support transactions still honours idempotence; one that
does buffers transactional records until ``commit_transaction``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.streaming.log.partition import FetchResult
from app.streaming.log.record import RecordMetadata, TopicPartition
from app.streaming.log.topic import TopicConfig

__all__ = [
    "Broker",
    "GroupDescription",
    "JoinResult",
    "MemberInfo",
    "ProduceContext",
]


@dataclass(frozen=True, slots=True)
class ProduceContext:
    """Exactly-once metadata threaded from a producer into ``Broker.produce``.

    All fields default to "plain produce" (no idempotence, no transaction). A
    :class:`~app.streaming.log.producer.Producer` configured idempotent sets
    ``producer_id``/``epoch``/``sequence``; a transactional one additionally sets
    ``transactional`` so the broker buffers the record until commit.
    """

    producer_id: str | None = None
    epoch: int = 0
    sequence: int | None = None
    transactional: bool = False
    transactional_id: str | None = None


@dataclass(frozen=True, slots=True)
class MemberInfo:
    """A consumer-group member and the partitions assigned to it."""

    member_id: str
    assignment: tuple[TopicPartition, ...]


@dataclass(frozen=True, slots=True)
class JoinResult:
    """The result of joining a consumer group.

    Carries the member's id (broker-assigned if the caller passed none), the
    group's current generation (a monotonic epoch bumped on every rebalance),
    and the member's partition assignment under that generation.
    """

    member_id: str
    generation: int
    assignment: tuple[TopicPartition, ...]
    is_leader: bool


@dataclass(frozen=True, slots=True)
class GroupDescription:
    """A snapshot of a consumer group's membership + generation."""

    group_id: str
    generation: int
    members: tuple[MemberInfo, ...]
    subscription: tuple[str, ...]


@runtime_checkable
class Broker(Protocol):
    """Async partitioned-log broker. See the module docstring for the contract."""

    # --- lifecycle ------------------------------------------------------- #

    async def start(self) -> None:
        """Open any connections / start background tasks (idempotent)."""
        ...

    async def close(self) -> None:
        """Release resources (idempotent)."""
        ...

    # --- admin ----------------------------------------------------------- #

    async def create_topic(self, config: TopicConfig) -> None:
        """Create a topic; raise ``TopicExistsError`` if it already exists."""
        ...

    async def delete_topic(self, topic: str) -> None:
        """Delete a topic and all its partitions/offsets."""
        ...

    async def topics(self) -> tuple[str, ...]:
        """All existing topic names."""
        ...

    async def describe_topic(self, topic: str) -> TopicConfig:
        """Return a topic's durable configuration."""
        ...

    async def partitions_for(self, topic: str) -> int:
        """Return a topic's partition count."""
        ...

    async def maintain(self, *, now: int | None = None) -> int:
        """Run retention + compaction across every partition; return records removed.

        ``now`` (epoch ms) overrides the wall clock for deterministic tests. The
        background :class:`~app.streaming.log.cleaner.LogCleaner` drives this on a
        schedule; callers may also invoke it directly.
        """
        ...

    # --- produce --------------------------------------------------------- #

    async def produce(
        self,
        topic: str,
        partition: int,
        *,
        key: bytes | None,
        value: bytes | None,
        timestamp_ms: int | None = None,
        headers: tuple[tuple[str, bytes], ...] = (),
        ctx: ProduceContext = ProduceContext(),
    ) -> RecordMetadata:
        """Append one resolved record to ``topic``-``partition``; return its metadata."""
        ...

    # --- consume --------------------------------------------------------- #

    async def fetch(
        self,
        topic: str,
        partition: int,
        offset: int,
        *,
        max_records: int = 500,
        max_bytes: int | None = None,
    ) -> FetchResult:
        """Read a window of records from one partition starting at ``offset``."""
        ...

    async def beginning_offsets(
        self, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int]:
        """The earliest readable offset per partition."""
        ...

    async def end_offsets(
        self, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int]:
        """The next-to-be-assigned offset (high watermark) per partition."""
        ...

    async def offsets_for_times(
        self, timestamps: dict[TopicPartition, int]
    ) -> dict[TopicPartition, int | None]:
        """First offset at/after each timestamp per partition (``None`` if none)."""
        ...

    # --- consumer-group offset store ------------------------------------ #

    async def commit_offsets(
        self,
        group_id: str,
        offsets: dict[TopicPartition, int],
        *,
        generation: int | None = None,
        member_id: str | None = None,
    ) -> None:
        """Persist a group's committed offsets (the *next* offset to read)."""
        ...

    async def fetch_committed(
        self, group_id: str, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int | None]:
        """Read a group's committed offsets (``None`` where it never committed)."""
        ...

    async def list_committed(self, group_id: str) -> dict[TopicPartition, int]:
        """All committed offsets for a group."""
        ...

    # --- consumer-group membership -------------------------------------- #

    async def join_group(
        self,
        group_id: str,
        *,
        member_id: str | None,
        subscription: tuple[str, ...],
        protocol: str = "range",
    ) -> JoinResult:
        """Join (or rejoin) a group; trigger a rebalance and return the assignment."""
        ...

    async def leave_group(self, group_id: str, member_id: str) -> None:
        """Leave a group, triggering a rebalance of its partitions."""
        ...

    async def heartbeat(self, group_id: str, member_id: str, generation: int) -> bool:
        """Liveness ping; returns ``False`` (rejoin needed) if mid-rebalance/stale."""
        ...

    async def describe_group(self, group_id: str) -> GroupDescription:
        """A snapshot of a group's membership + generation."""
        ...
