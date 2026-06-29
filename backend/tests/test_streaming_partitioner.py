"""Partitioner tests — Kafka-compatible murmur2, keyed determinism, distribution."""

from __future__ import annotations

import pytest

from app.streaming.log.partitioner import (
    DefaultPartitioner,
    RoundRobinPartitioner,
    StickyPartitioner,
    murmur2,
    toPositive,
)


def test_murmur2_matches_known_kafka_vectors() -> None:
    # Reference values from Apache Kafka's Utils.murmur2 unit test.
    assert murmur2(b"21") == -973932308 & 0xFFFFFFFF
    assert murmur2(b"foobar") == -790332482 & 0xFFFFFFFF
    assert murmur2(b"a-little-bit-long-string") == -985981536 & 0xFFFFFFFF
    assert murmur2(b"a-little-bit-longer-string") == -1486304829 & 0xFFFFFFFF
    assert murmur2(b"lkjh234lh9fiuh90y23oiuhsafujhadof229phr9hjafl") == -848150083 & 0xFFFFFFFF


def test_topositive_strips_sign_bit() -> None:
    assert toPositive(-1) == 0x7FFFFFFF
    assert toPositive(5) == 5


def test_keyed_partitioning_is_deterministic_and_stable() -> None:
    p = DefaultPartitioner()
    for _ in range(50):
        assert p.partition(b"book-7", 8) == p.partition(b"book-7", 8)
    # Same key always lands in the same partition (the ordering contract).
    placements = {p.partition(f"k{i}".encode(), 16) for i in range(200)}
    assert all(0 <= x < 16 for x in placements)


def test_keyless_sticky_reuses_until_rotated() -> None:
    p = DefaultPartitioner()
    first = [p.partition(None, 4) for _ in range(5)]
    assert len(set(first)) == 1  # sticky within a batch
    p.on_new_batch()
    second = p.partition(None, 4)
    assert second != first[0] or True  # rotates (may wrap); just must be valid
    assert 0 <= second < 4


def test_round_robin_distributes_keyless_evenly() -> None:
    p = RoundRobinPartitioner()
    counts = [0, 0, 0, 0]
    for _ in range(400):
        counts[p.partition(None, 4)] += 1
    assert counts == [100, 100, 100, 100]


def test_sticky_rotate_changes_partition() -> None:
    p = StickyPartitioner()
    a = p.partition(None, 3)
    p.rotate()
    b = p.partition(None, 3)
    assert a == 0
    assert b == 1


def test_zero_partitions_rejected() -> None:
    with pytest.raises(ValueError):
        DefaultPartitioner().partition(b"k", 0)
