"""Record value-object tests — construction helpers, tombstones, decoding."""

from __future__ import annotations

from app.streaming.log.record import (
    ConsumerRecord,
    ProducerRecord,
    RecordMetadata,
    TopicPartition,
)


def test_topic_partition_is_ordered_and_hashable() -> None:
    a = TopicPartition("t", 0)
    b = TopicPartition("t", 1)
    c = TopicPartition("s", 5)
    assert a < b
    assert c < a  # "s" < "t"
    assert {a, b, c, TopicPartition("t", 0)} == {a, b, c}
    assert str(a) == "t-0"


def test_producer_record_from_str_encodes_utf8() -> None:
    rec = ProducerRecord.from_str("beats", "héllo", key="book-7")
    assert rec.value == "héllo".encode()
    assert rec.key == b"book-7"
    assert not rec.is_tombstone


def test_producer_record_from_json_roundtrip() -> None:
    rec = ProducerRecord.from_json("beats", {"page": 12, "k": [1, 2]}, key="b")
    assert rec.value is not None
    import json

    assert json.loads(rec.value) == {"page": 12, "k": [1, 2]}


def test_json_none_value_is_a_true_tombstone() -> None:
    rec = ProducerRecord.from_json("beats", None, key="gone")
    assert rec.value is None
    assert rec.is_tombstone


def test_consumer_record_decoders() -> None:
    rec = ConsumerRecord(
        topic="t",
        partition=2,
        offset=9,
        timestamp_ms=1000,
        key=b"k",
        value=b'{"x":1}',
        headers=(("trace", b"abc"),),
    )
    assert rec.topic_partition == TopicPartition("t", 2)
    assert rec.key_str() == "k"
    assert rec.value_str() == '{"x":1}'
    assert rec.json() == {"x": 1}
    assert not rec.is_tombstone


def test_consumer_tombstone_detection() -> None:
    rec = ConsumerRecord(topic="t", partition=0, offset=3, timestamp_ms=1, key=b"k", value=None)
    assert rec.is_tombstone
    assert rec.json() is None


def test_record_metadata_topic_partition() -> None:
    meta = RecordMetadata(topic="t", partition=4, offset=11, timestamp_ms=42)
    assert meta.topic_partition == TopicPartition("t", 4)
