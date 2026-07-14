"""The :class:`Broker` boundary to facet A, with an in-memory fallback.

This module defines the small synchronous broker contract used by the in-process
processing engine. The production streaming log has a broader asynchronous
broker contract; adapters between the two should be explicit rather than
changing this module's exported protocol at import time.

The fallback :class:`InMemoryBroker` is an offset-addressed, partition-aware log
sufficient for the deterministic test driver and local pipeline runs. Source and
sink connectors (:class:`BrokerSource`, :class:`BrokerSink`) bridge a topic to /
from the processing engine's :class:`StreamRecord` model.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Generic, Protocol, TypeVar, runtime_checkable

from app.streaming.processing.records import StreamRecord

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BrokerMessage(Generic[T]):
    """One log entry: its partition, offset, value, and producer timestamp (ms)."""

    topic: str
    partition: int
    offset: int
    value: T
    timestamp_ms: int
    key: str | None = None


@runtime_checkable
class Broker(Protocol):
    """The append-only-log contract facet B consumes from facet A.

    Deliberately small: a publish, an offset-addressed read, a high-watermark
    query, and an offset commit. Facet A may expose a richer surface; facet B
    only needs this slice. ``read`` returns up to ``max_records`` messages from
    ``offset`` (inclusive) on ``partition``, in offset order.
    """

    def publish(
        self, topic: str, value: object, *, timestamp_ms: int, key: str | None = ...
    ) -> int: ...

    def read(
        self, topic: str, partition: int, offset: int, *, max_records: int = ...
    ) -> list[BrokerMessage[object]]: ...

    def high_watermark(self, topic: str, partition: int) -> int: ...

    def partitions(self, topic: str) -> int: ...

    def commit(self, group: str, topic: str, partition: int, offset: int) -> None: ...

    def committed(self, group: str, topic: str, partition: int) -> int: ...


@dataclass
class _Partition(Generic[T]):
    """A single partition's ordered message list."""

    messages: list[BrokerMessage[T]] = field(default_factory=list)


class InMemoryBroker:
    """A minimal, partition-aware, offset-addressed in-memory log.

    Hash-partitions by ``key`` (round-robins when no key). Offsets are dense and
    zero-based per partition. Consumer-group offsets are tracked so a pipeline
    can resume from where it committed — the same at-least-once → exactly-once
    handshake the checkpoint coordinator relies on (commit offsets *with* the
    state checkpoint, replay from the committed offset on recovery).
    """

    def __init__(self, *, partitions: int = 1) -> None:
        if partitions < 1:
            raise ValueError("a topic needs at least one partition")
        self._n = partitions
        self._topics: dict[str, list[_Partition[object]]] = {}
        self._offsets: dict[tuple[str, str, int], int] = {}
        self._rr = 0

    def _ensure(self, topic: str) -> list[_Partition[object]]:
        if topic not in self._topics:
            self._topics[topic] = [_Partition() for _ in range(self._n)]
        return self._topics[topic]

    def _partition_for(self, key: str | None) -> int:
        if key is None:
            p = self._rr % self._n
            self._rr += 1
            return p
        return hash(key) % self._n

    def publish(
        self, topic: str, value: object, *, timestamp_ms: int, key: str | None = None
    ) -> int:
        parts = self._ensure(topic)
        p = self._partition_for(key)
        offset = len(parts[p].messages)
        parts[p].messages.append(
            BrokerMessage(
                topic=topic,
                partition=p,
                offset=offset,
                value=value,
                timestamp_ms=timestamp_ms,
                key=key,
            )
        )
        return offset

    def read(
        self, topic: str, partition: int, offset: int, *, max_records: int = 1024
    ) -> list[BrokerMessage[object]]:
        parts = self._ensure(topic)
        msgs = parts[partition].messages
        return msgs[offset : offset + max_records]

    def high_watermark(self, topic: str, partition: int) -> int:
        return len(self._ensure(topic)[partition].messages)

    def partitions(self, topic: str) -> int:
        return self._n

    def commit(self, group: str, topic: str, partition: int, offset: int) -> None:
        self._offsets[(group, topic, partition)] = offset

    def committed(self, group: str, topic: str, partition: int) -> int:
        return self._offsets.get((group, topic, partition), 0)

    def iter_all(self, topic: str) -> Iterator[BrokerMessage[object]]:
        """Iterate every message across partitions in (partition, offset) order.

        A convenience for tests and replay; production consumers use ``read`` +
        committed offsets.
        """

        for part in self._ensure(topic):
            yield from part.messages


@dataclass(slots=True)
class BrokerSource:
    """Bridges a broker topic into the engine as :class:`StreamRecord`\\ s.

    Reads from the consumer group's committed offset across all partitions and
    maps each :class:`BrokerMessage` to a record stamped with the broker
    timestamp (which a watermark strategy may override with a payload field).
    """

    broker: Broker
    topic: str
    group: str

    def poll(self, *, max_records: int = 1024) -> list[StreamRecord[object]]:
        records: list[StreamRecord[object]] = []
        for p in range(self.broker.partitions(self.topic)):
            start = self.broker.committed(self.group, self.topic, p)
            for msg in self.broker.read(self.topic, p, start, max_records=max_records):
                records.append(StreamRecord(value=msg.value, timestamp=msg.timestamp_ms))
            hw = self.broker.high_watermark(self.topic, p)
            if hw > start:
                self.broker.commit(self.group, self.topic, p, hw)
        records.sort(key=lambda r: r.timestamp)
        return records


@dataclass(slots=True)
class BrokerSink(Generic[T]):
    """Publishes engine output records back onto a broker topic.

    Idempotent against replay when paired with the checkpoint coordinator: the
    sink only publishes records produced after the last committed checkpoint, so
    re-running from a checkpoint never double-writes. Here it simply forwards;
    the runtime gates which records reach it.
    """

    broker: Broker
    topic: str
    key_fn: object = None  # Callable[[T], str | None]; kept loose for the protocol

    def emit(self, record: StreamRecord[T]) -> int:
        key: str | None = None
        if callable(self.key_fn):
            key = self.key_fn(record.value)
        return self.broker.publish(
            self.topic, record.value, timestamp_ms=record.timestamp, key=key
        )
