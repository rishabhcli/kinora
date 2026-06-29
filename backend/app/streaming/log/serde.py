"""Typed (de)serialization seam — turn domain values into records and back.

The log core is payload-agnostic: keys/values are ``bytes``. The sibling facets
(processing, CDC) want to work in *domain types* (dicts, dataclasses, strings)
without re-implementing encode/decode at every call-site. :class:`Serde` is that
seam — a paired serializer + deserializer — and :class:`TypedProducer` /
:class:`TypedConsumer` wrap the raw :class:`~app.streaming.log.producer.Producer`
/ :class:`~app.streaming.log.consumer.Consumer` so a facet sends and receives
``T`` while the substrate still sees ``bytes``.

Built-in serdes cover the common cases: ``str`` (UTF-8), JSON, and raw ``bytes``
passthrough. A facet needing Avro/Protobuf supplies its own :class:`Serde` — the
contract is just two pure functions, so a schema registry can live behind it
without the log knowing.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.streaming.log.consumer import Consumer
from app.streaming.log.producer import Producer
from app.streaming.log.record import ConsumerRecord, ProducerRecord, RecordMetadata

__all__ = [
    "BytesSerde",
    "JsonSerde",
    "Serde",
    "StringSerde",
    "TypedConsumer",
    "TypedProducer",
    "TypedRecord",
]

T = TypeVar("T")
K = TypeVar("K")
V = TypeVar("V")


@dataclass(frozen=True, slots=True)
class Serde(Generic[T]):
    """A paired ``serialize`` / ``deserialize`` for one domain type.

    ``serialize(None)`` must return ``None`` (so tombstones round-trip), and a
    well-behaved serde is symmetric: ``deserialize(serialize(x)) == x``.
    """

    serialize: Callable[[T | None], bytes | None]
    deserialize: Callable[[bytes | None], T | None]


def _str_ser(value: str | None) -> bytes | None:
    return None if value is None else value.encode("utf-8")


def _str_de(data: bytes | None) -> str | None:
    return None if data is None else data.decode("utf-8")


def _json_ser(value: object | None) -> bytes | None:
    return None if value is None else json.dumps(value, separators=(",", ":")).encode("utf-8")


def _json_de(data: bytes | None) -> object | None:
    return None if data is None else json.loads(data)


def _bytes_ser(value: bytes | None) -> bytes | None:
    return value


def _bytes_de(data: bytes | None) -> bytes | None:
    return data


#: UTF-8 string serde.
StringSerde: Serde[str] = Serde(serialize=_str_ser, deserialize=_str_de)
#: Compact-JSON serde (any JSON-encodable value).
JsonSerde: Serde[object] = Serde(serialize=_json_ser, deserialize=_json_de)
#: Identity serde for callers already holding ``bytes``.
BytesSerde: Serde[bytes] = Serde(serialize=_bytes_ser, deserialize=_bytes_de)


@dataclass(frozen=True, slots=True)
class TypedRecord(Generic[K, V]):
    """A consumer record decoded into domain key/value types."""

    topic: str
    partition: int
    offset: int
    timestamp_ms: int
    key: K | None
    value: V | None

    @classmethod
    def of(
        cls, record: ConsumerRecord, key_serde: Serde[K], value_serde: Serde[V]
    ) -> TypedRecord[K, V]:
        """Decode a raw :class:`ConsumerRecord` with the given serdes."""
        return cls(
            topic=record.topic,
            partition=record.partition,
            offset=record.offset,
            timestamp_ms=record.timestamp_ms,
            key=key_serde.deserialize(record.key),
            value=value_serde.deserialize(record.value),
        )


class TypedProducer(Generic[K, V]):
    """A :class:`Producer` that serializes domain key/value types on send."""

    def __init__(
        self, producer: Producer, *, key_serde: Serde[K], value_serde: Serde[V]
    ) -> None:
        self._producer = producer
        self._key_serde = key_serde
        self._value_serde = value_serde

    @property
    def raw(self) -> Producer:
        """The wrapped raw producer (escape hatch for transactions etc.)."""
        return self._producer

    async def send(
        self,
        topic: str,
        value: V | None,
        *,
        key: K | None = None,
        partition: int | None = None,
    ) -> RecordMetadata:
        """Serialize ``key``/``value`` and produce, awaiting the durable metadata."""
        record = ProducerRecord(
            topic=topic,
            value=self._value_serde.serialize(value),
            key=self._key_serde.serialize(key),
            partition=partition,
        )
        return await self._producer.send_and_wait(record)

    async def flush(self) -> None:
        """Flush the underlying producer's buffered batches."""
        await self._producer.flush()

    async def close(self) -> None:
        """Close the underlying producer."""
        await self._producer.close()


class TypedConsumer(Generic[K, V]):
    """A :class:`Consumer` that deserializes records into domain types on poll."""

    def __init__(
        self, consumer: Consumer, *, key_serde: Serde[K], value_serde: Serde[V]
    ) -> None:
        self._consumer = consumer
        self._key_serde = key_serde
        self._value_serde = value_serde

    @property
    def raw(self) -> Consumer:
        """The wrapped raw consumer (escape hatch for assign/subscribe/commit)."""
        return self._consumer

    async def poll(self, *, max_records: int | None = None) -> list[TypedRecord[K, V]]:
        """Poll a batch and decode each record into a :class:`TypedRecord`."""
        batch = await self._consumer.poll(max_records=max_records)
        return [TypedRecord.of(r, self._key_serde, self._value_serde) for r in batch]
