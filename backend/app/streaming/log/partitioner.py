"""Partition selection — how a record's key maps to a partition.

Keyed partitioning is the contract that makes per-key ordering possible: all
records sharing a key land in the same partition, so a consumer reading that
partition sees them in append order. The default :class:`DefaultPartitioner`
reproduces Kafka's scheme exactly — **murmur2** of the key masked to a positive
int, modulo the partition count — so a topic produced here and re-read by a real
Kafka client (or vice versa) agrees on placement.

Keyless records use a *sticky* round-robin: a partition is chosen and reused
for a batch, then rotated, which keeps batches dense (Kafka's sticky
partitioner) without starving any partition over time.

All partitioners are pure given their inputs except :class:`StickyPartitioner`,
which holds a tiny rotation cursor; that state is explicit and resettable.
"""

from __future__ import annotations

import itertools
from typing import Protocol, runtime_checkable

__all__ = [
    "DefaultPartitioner",
    "Partitioner",
    "RoundRobinPartitioner",
    "StickyPartitioner",
    "murmur2",
    "toPositive",
]

_UINT32 = 0xFFFFFFFF


def murmur2(data: bytes) -> int:
    """Kafka's 32-bit MurmurHash2 (seed ``0x9747b28c``), as an unsigned int.

    A faithful port of ``org.apache.kafka.common.utils.Utils.murmur2`` so key→
    partition placement is wire-compatible with the JVM and librdkafka clients.
    """
    length = len(data)
    seed = 0x9747B28C
    m = 0x5BD1E995
    r = 24

    h = (seed ^ length) & _UINT32
    length4 = length // 4

    for i in range(length4):
        i4 = i * 4
        k = (
            (data[i4 + 0] & 0xFF)
            | ((data[i4 + 1] & 0xFF) << 8)
            | ((data[i4 + 2] & 0xFF) << 16)
            | ((data[i4 + 3] & 0xFF) << 24)
        ) & _UINT32
        k = (k * m) & _UINT32
        k ^= (k & _UINT32) >> r
        k = (k * m) & _UINT32
        h = (h * m) & _UINT32
        h ^= k

    # Tail bytes.
    rem = length & 3
    if rem == 3:
        h ^= (data[(length & ~3) + 2] & 0xFF) << 16
    if rem >= 2:
        h ^= (data[(length & ~3) + 1] & 0xFF) << 8
    if rem >= 1:
        h ^= data[length & ~3] & 0xFF
        h = (h * m) & _UINT32

    h ^= (h & _UINT32) >> 13
    h = (h * m) & _UINT32
    h ^= (h & _UINT32) >> 15
    return h & _UINT32


def toPositive(value: int) -> int:  # noqa: N802 - mirrors Kafka's Utils.toPositive
    """Mask to a non-negative 31-bit int — Kafka's ``Utils.toPositive``."""
    return value & 0x7FFFFFFF


@runtime_checkable
class Partitioner(Protocol):
    """Chooses a partition index in ``[0, num_partitions)`` for a record."""

    def partition(self, key: bytes | None, num_partitions: int) -> int:
        """Return the partition index for ``key`` over ``num_partitions``."""
        ...


class DefaultPartitioner:
    """Kafka-compatible: ``murmur2(key) % n`` for keyed, sticky for keyless.

    Holds the sticky cursor used for keyless records, so a single instance gives
    Kafka's "dense batches, fair over time" behaviour. Keyed placement is pure.
    """

    def __init__(self) -> None:
        self._sticky = StickyPartitioner()

    def partition(self, key: bytes | None, num_partitions: int) -> int:
        if num_partitions <= 0:
            raise ValueError("num_partitions must be positive")
        if key is None:
            return self._sticky.partition(key, num_partitions)
        return toPositive(murmur2(key)) % num_partitions

    def on_new_batch(self) -> None:
        """Advance the sticky cursor (call when a keyless batch is flushed)."""
        self._sticky.rotate()


class RoundRobinPartitioner:
    """Keyed records hash (Kafka-compatible); keyless records round-robin every call.

    Distributes keyless load uniformly per-record rather than per-batch — useful
    for tests and low-throughput producers where batch density doesn't matter.
    """

    def __init__(self) -> None:
        self._counter = itertools.count()

    def partition(self, key: bytes | None, num_partitions: int) -> int:
        if num_partitions <= 0:
            raise ValueError("num_partitions must be positive")
        if key is not None:
            return toPositive(murmur2(key)) % num_partitions
        return next(self._counter) % num_partitions


class StickyPartitioner:
    """Reuses one partition for keyless records until :meth:`rotate` advances it.

    Keyed records still hash (Kafka-compatible). The chosen partition is lazily
    initialised on first keyless use and changes only on :meth:`rotate`.
    """

    def __init__(self, *, start: int = 0) -> None:
        self._current: int | None = None
        self._rotations = start

    def partition(self, key: bytes | None, num_partitions: int) -> int:
        if num_partitions <= 0:
            raise ValueError("num_partitions must be positive")
        if key is not None:
            return toPositive(murmur2(key)) % num_partitions
        if self._current is None or self._current >= num_partitions:
            self._current = self._rotations % num_partitions
        return self._current

    def rotate(self) -> None:
        """Pick a new sticky partition for the next keyless batch."""
        self._rotations += 1
        self._current = None
