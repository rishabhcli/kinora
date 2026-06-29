"""Record value objects — the units that flow through the log.

Kafka's record model, trimmed to what this substrate needs:

* :class:`ProducerRecord` — what a producer *sends*: an optional key (drives
  partitioning + compaction), a value, headers, an optional explicit partition,
  and an optional timestamp. Keys/values are ``bytes`` on the wire; helpers
  build them from ``str`` or JSON.
* :class:`RecordMetadata` — the durable acknowledgement a producer gets back:
  the topic-partition the record landed in and the offset it was assigned.
* :class:`ConsumerRecord` — what a consumer *reads*: everything in the producer
  record plus its assigned offset, partition, and the append timestamp. A
  ``None`` value at a non-zero offset is a *tombstone* (compaction delete).

All three are frozen dataclasses (hashable, cheap to pass around). Keys and
values are raw ``bytes`` so the log is payload-agnostic; the JSON helpers keep
the Kinora call-sites ergonomic without baking a codec into the core.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "ConsumerRecord",
    "Headers",
    "ProducerRecord",
    "RecordMetadata",
    "TopicPartition",
    "now_ms",
]

#: Header list: ordered key→value pairs (values are bytes), Kafka-style.
Headers = tuple[tuple[str, bytes], ...]


def now_ms() -> int:
    """Current wall-clock time in integer milliseconds (record timestamps)."""
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True, order=True)
class TopicPartition:
    """A ``(topic, partition)`` coordinate — the addressing unit of the log.

    Ordered so collections of partitions sort deterministically (range
    assignment, stable rebalance output, reproducible tests).
    """

    topic: str
    partition: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.topic}-{self.partition}"


@dataclass(frozen=True, slots=True)
class ProducerRecord:
    """A record a producer hands to the broker for appending.

    ``partition`` may be ``None`` (the partitioner decides) or an explicit
    index (the partitioner is bypassed). ``timestamp_ms`` defaults to append
    time when ``None``.
    """

    topic: str
    value: bytes | None
    key: bytes | None = None
    partition: int | None = None
    timestamp_ms: int | None = None
    headers: Headers = ()

    @classmethod
    def from_str(
        cls,
        topic: str,
        value: str | None,
        *,
        key: str | None = None,
        partition: int | None = None,
        headers: Headers = (),
        encoding: str = "utf-8",
    ) -> ProducerRecord:
        """Build a record from ``str`` key/value (UTF-8 by default)."""
        return cls(
            topic=topic,
            value=None if value is None else value.encode(encoding),
            key=None if key is None else key.encode(encoding),
            partition=partition,
            headers=headers,
        )

    @classmethod
    def from_json(
        cls,
        topic: str,
        value: Any,
        *,
        key: str | None = None,
        partition: int | None = None,
        headers: Headers = (),
    ) -> ProducerRecord:
        """Build a record whose value is the compact-JSON encoding of ``value``.

        A ``None`` value is preserved as a true tombstone (not JSON ``"null"``),
        so JSON-valued compacted topics can still delete keys.
        """
        encoded = None if value is None else json.dumps(value, separators=(",", ":")).encode()
        return cls(
            topic=topic,
            value=encoded,
            key=None if key is None else key.encode("utf-8"),
            partition=partition,
            headers=headers,
        )

    @property
    def is_tombstone(self) -> bool:
        """A keyed record with a ``None`` value — a compaction delete marker."""
        return self.value is None and self.key is not None


@dataclass(frozen=True, slots=True)
class RecordMetadata:
    """The durable acknowledgement returned after a successful append."""

    topic: str
    partition: int
    offset: int
    timestamp_ms: int

    @property
    def topic_partition(self) -> TopicPartition:
        """The ``(topic, partition)`` this record landed in."""
        return TopicPartition(self.topic, self.partition)


@dataclass(frozen=True, slots=True)
class ConsumerRecord:
    """A record as read back from the log, with its assigned coordinates."""

    topic: str
    partition: int
    offset: int
    timestamp_ms: int
    key: bytes | None = None
    value: bytes | None = None
    headers: Headers = field(default=())

    @property
    def topic_partition(self) -> TopicPartition:
        """The ``(topic, partition)`` this record was read from."""
        return TopicPartition(self.topic, self.partition)

    @property
    def is_tombstone(self) -> bool:
        """A keyed record with a ``None`` value — a compaction delete marker."""
        return self.value is None and self.key is not None

    def key_str(self, encoding: str = "utf-8") -> str | None:
        """Decode the key as ``str`` (``None`` passthrough)."""
        return None if self.key is None else self.key.decode(encoding)

    def value_str(self, encoding: str = "utf-8") -> str | None:
        """Decode the value as ``str`` (``None`` passthrough)."""
        return None if self.value is None else self.value.decode(encoding)

    def json(self) -> Any:
        """Decode the value as JSON (``None`` passthrough)."""
        return None if self.value is None else json.loads(self.value)
