"""Facet A — the partitioned log: a Kafka/Redpanda-shaped streaming substrate.

A general, replayable, offset-addressed event log (distinct from the render
queue in :mod:`app.queue`). Topics carry ordered, keyed records across
partitions; producers append idempotently/transactionally; consumer *groups*
read at their own pace with automatic partition rebalancing and durable offset
commits; exactly-once semantics tie a set of appends to the consumer offsets
that produced them.

The whole thing is written against one seam — the
:class:`~app.streaming.log.broker.Broker` protocol — with two implementations:

* :class:`~app.streaming.log.memory.InMemoryBroker` — zero-infra, for tests and
  single-process deployments.
* :class:`~app.streaming.log.redis.RedisStreamsBroker` — Redis-Streams-backed,
  for production / multi-process.

See ``DESIGN.md`` for the protocol contract the two sibling facets (processing,
CDC) consume.

Typical use::

    broker = InMemoryBroker()
    await broker.start()
    await broker.create_topic(TopicConfig.deleted("beats", partitions=4))

    producer = Producer(broker)
    await producer.send_and_wait(ProducerRecord.from_json("beats", {"page": 12}, key="book-7"))

    consumer = Consumer(broker, config=ConsumerConfig(group_id="renderers"))
    await consumer.subscribe(("beats",))
    for record in await consumer.poll():
        handle(record.json())
    await consumer.commit()
"""

from __future__ import annotations

from app.streaming.log.admin import (
    Admin,
    GroupLag,
    PartitionOffsets,
    TopicDescription,
)
from app.streaming.log.broker import (
    Broker,
    GroupDescription,
    JoinResult,
    MemberInfo,
    ProduceContext,
)
from app.streaming.log.cleaner import CleanerStats, LogCleaner
from app.streaming.log.consumer import AutoOffsetReset, Consumer, ConsumerConfig
from app.streaming.log.errors import (
    CommitConflictError,
    FencedProducerError,
    IllegalStateError,
    InvalidConfigError,
    OffsetOutOfRangeError,
    PartitionNotFoundError,
    ProducerError,
    RebalanceInProgressError,
    RecordTooLargeError,
    SequenceError,
    StreamingError,
    TopicExistsError,
    TopicNotFoundError,
    TransactionError,
    UnknownMemberError,
)
from app.streaming.log.group import (
    Assignor,
    CooperativeStickyAssignor,
    GroupCoordinator,
    RangeAssignor,
    RoundRobinAssignor,
    get_assignor,
)
from app.streaming.log.memory import InMemoryBroker
from app.streaming.log.metrics import (
    InMemoryMetrics,
    MetricSnapshot,
    MetricsSink,
    NullMetrics,
)
from app.streaming.log.partition import FetchResult, PartitionLog
from app.streaming.log.partitioner import (
    DefaultPartitioner,
    Partitioner,
    RoundRobinPartitioner,
    StickyPartitioner,
    murmur2,
)
from app.streaming.log.producer import Producer, ProducerConfig
from app.streaming.log.prometheus import PrometheusMetrics
from app.streaming.log.record import (
    ConsumerRecord,
    Headers,
    ProducerRecord,
    RecordMetadata,
    TopicPartition,
    now_ms,
)
from app.streaming.log.serde import (
    BytesSerde,
    JsonSerde,
    Serde,
    StringSerde,
    TypedConsumer,
    TypedProducer,
    TypedRecord,
)
from app.streaming.log.topic import (
    CleanupPolicy,
    RetentionPolicy,
    TopicConfig,
)

__all__ = [
    "Admin",
    "Assignor",
    "AutoOffsetReset",
    "Broker",
    "BytesSerde",
    "CleanerStats",
    "CleanupPolicy",
    "CommitConflictError",
    "Consumer",
    "ConsumerConfig",
    "ConsumerRecord",
    "CooperativeStickyAssignor",
    "DefaultPartitioner",
    "FencedProducerError",
    "FetchResult",
    "GroupCoordinator",
    "GroupDescription",
    "GroupLag",
    "Headers",
    "IllegalStateError",
    "InMemoryBroker",
    "InMemoryMetrics",
    "InvalidConfigError",
    "JoinResult",
    "JsonSerde",
    "LogCleaner",
    "MemberInfo",
    "MetricSnapshot",
    "MetricsSink",
    "NullMetrics",
    "OffsetOutOfRangeError",
    "PartitionLog",
    "PartitionNotFoundError",
    "PartitionOffsets",
    "Partitioner",
    "ProduceContext",
    "Producer",
    "ProducerConfig",
    "ProducerError",
    "ProducerRecord",
    "PrometheusMetrics",
    "RangeAssignor",
    "RebalanceInProgressError",
    "RecordMetadata",
    "RecordTooLargeError",
    "RetentionPolicy",
    "RoundRobinAssignor",
    "RoundRobinPartitioner",
    "Serde",
    "SequenceError",
    "StickyPartitioner",
    "StreamingError",
    "StringSerde",
    "TopicConfig",
    "TopicDescription",
    "TopicExistsError",
    "TopicNotFoundError",
    "TopicPartition",
    "TransactionError",
    "TypedConsumer",
    "TypedProducer",
    "TypedRecord",
    "UnknownMemberError",
    "get_assignor",
    "murmur2",
    "now_ms",
]
