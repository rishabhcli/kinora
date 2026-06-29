"""The admin client — ergonomic topic management + group/lag introspection.

:class:`Admin` is a thin convenience layer over the :class:`~app.streaming.log.
broker.Broker` protocol for operational tasks: idempotent topic creation,
listing/describing, and the introspection the sibling facets (and a future
metrics endpoint) need — per-partition offsets, a consumer group's lag, and the
end-to-end "total records currently in a topic" count.

Nothing here adds state; it composes existing broker calls into the queries an
operator actually asks ("how far behind is group *g*?"). Keeping it separate from
the broker keeps the protocol minimal while still giving callers batteries.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.streaming.log.broker import Broker, GroupDescription
from app.streaming.log.errors import TopicNotFoundError
from app.streaming.log.record import TopicPartition
from app.streaming.log.topic import TopicConfig

__all__ = ["Admin", "GroupLag", "PartitionOffsets", "TopicDescription"]


@dataclass(frozen=True, slots=True)
class PartitionOffsets:
    """The offset window of a single partition."""

    partition: int
    log_start_offset: int
    log_end_offset: int

    @property
    def record_count(self) -> int:
        """Records currently retained (end - start)."""
        return self.log_end_offset - self.log_start_offset


@dataclass(frozen=True, slots=True)
class TopicDescription:
    """A topic's config plus the live offset window of each partition."""

    config: TopicConfig
    partitions: tuple[PartitionOffsets, ...]

    @property
    def total_records(self) -> int:
        """Total retained records across all partitions."""
        return sum(p.record_count for p in self.partitions)


@dataclass(frozen=True, slots=True)
class GroupLag:
    """A consumer group's lag per partition (and the total)."""

    group_id: str
    generation: int
    per_partition: dict[TopicPartition, int]

    @property
    def total(self) -> int:
        """Sum of lag across every partition the group reads."""
        return sum(self.per_partition.values())


class Admin:
    """Operational helpers over a :class:`Broker`."""

    def __init__(self, broker: Broker) -> None:
        self._broker = broker

    # --- topic management ----------------------------------------------- #

    async def create_topic_if_absent(self, config: TopicConfig) -> bool:
        """Create ``config``'s topic unless it exists; return whether it was created."""
        if config.name in await self._broker.topics():
            return False
        await self._broker.create_topic(config)
        return True

    async def ensure_topics(self, *configs: TopicConfig) -> list[str]:
        """Create any of ``configs`` that are missing; return the names created."""
        existing = set(await self._broker.topics())
        created: list[str] = []
        for config in configs:
            if config.name not in existing:
                await self._broker.create_topic(config)
                created.append(config.name)
        return created

    # --- introspection -------------------------------------------------- #

    async def _topic_partitions(self, topic: str) -> tuple[TopicPartition, ...]:
        n = await self._broker.partitions_for(topic)
        return tuple(TopicPartition(topic, p) for p in range(n))

    async def describe(self, topic: str) -> TopicDescription:
        """Full description of a topic: config + per-partition offset windows."""
        config = await self._broker.describe_topic(topic)
        tps = await self._topic_partitions(topic)
        starts = await self._broker.beginning_offsets(tps)
        ends = await self._broker.end_offsets(tps)
        partitions = tuple(
            PartitionOffsets(
                partition=tp.partition,
                log_start_offset=starts[tp],
                log_end_offset=ends[tp],
            )
            for tp in tps
        )
        return TopicDescription(config=config, partitions=partitions)

    async def topic_size(self, topic: str) -> int:
        """Total retained records across a topic's partitions."""
        return (await self.describe(topic)).total_records

    async def group(self, group_id: str) -> GroupDescription:
        """Membership snapshot for a consumer group."""
        return await self._broker.describe_group(group_id)

    async def group_lag(self, group_id: str, topic: str) -> GroupLag:
        """Compute a group's lag on ``topic`` (end-offset minus committed offset).

        A partition the group never committed counts its full retained record
        count as lag (it has consumed nothing). Raises if the topic is unknown.
        """
        if topic not in await self._broker.topics():
            raise TopicNotFoundError(topic)
        tps = await self._topic_partitions(topic)
        ends = await self._broker.end_offsets(tps)
        starts = await self._broker.beginning_offsets(tps)
        committed = await self._broker.fetch_committed(group_id, tps)
        generation = (await self._broker.describe_group(group_id)).generation
        per_partition: dict[TopicPartition, int] = {}
        for tp in tps:
            commit = committed[tp]
            position = commit if commit is not None else starts[tp]
            per_partition[tp] = max(0, ends[tp] - position)
        return GroupLag(group_id=group_id, generation=generation, per_partition=per_partition)
