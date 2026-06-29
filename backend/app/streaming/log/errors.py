"""Typed domain errors for the partitioned log.

Every failure mode the substrate can surface has a named exception so callers
(and the sibling facets) can branch on *kind* rather than parsing strings. All
inherit :class:`StreamingError` so a caller can catch the whole domain at once.

The hierarchy mirrors the Kafka error families it emulates: topic/partition
existence, offset bounds, producer fencing/idempotency, transaction state,
consumer-group membership/rebalance, and commit conflicts.
"""

from __future__ import annotations

__all__ = [
    "CommitConflictError",
    "FencedProducerError",
    "IllegalStateError",
    "InvalidConfigError",
    "OffsetOutOfRangeError",
    "PartitionNotFoundError",
    "ProducerError",
    "RebalanceInProgressError",
    "RecordTooLargeError",
    "SequenceError",
    "StreamingError",
    "TopicExistsError",
    "TopicNotFoundError",
    "TransactionError",
    "UnknownMemberError",
]


class StreamingError(Exception):
    """Base class for every error raised by the streaming log substrate."""


class InvalidConfigError(StreamingError, ValueError):
    """A topic/partition/producer/consumer configuration is invalid."""


# --------------------------------------------------------------------------- #
# Topic / partition existence + addressing
# --------------------------------------------------------------------------- #


class TopicNotFoundError(StreamingError, KeyError):
    """Operation referenced a topic that does not exist on the broker."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        super().__init__(f"unknown topic {topic!r}")


class TopicExistsError(StreamingError):
    """``create_topic`` was asked to create a topic that already exists."""

    def __init__(self, topic: str) -> None:
        self.topic = topic
        super().__init__(f"topic {topic!r} already exists")


class PartitionNotFoundError(StreamingError, KeyError):
    """Operation referenced a partition index outside a topic's range."""

    def __init__(self, topic: str, partition: int) -> None:
        self.topic = topic
        self.partition = partition
        super().__init__(f"topic {topic!r} has no partition {partition}")


class OffsetOutOfRangeError(StreamingError):
    """A fetch/seek targeted an offset outside ``[log_start, log_end)``.

    Carries the partition coordinates and the valid window so callers can apply
    an ``auto.offset.reset`` policy (earliest/latest) deterministically.
    """

    def __init__(
        self, topic: str, partition: int, offset: int, log_start: int, log_end: int
    ) -> None:
        self.topic = topic
        self.partition = partition
        self.offset = offset
        self.log_start = log_start
        self.log_end = log_end
        super().__init__(
            f"offset {offset} out of range for {topic}-{partition} "
            f"(valid [{log_start}, {log_end}))"
        )


class RecordTooLargeError(StreamingError):
    """A single record's serialized size exceeds the topic's ``max_message_bytes``."""

    def __init__(self, size: int, limit: int) -> None:
        self.size = size
        self.limit = limit
        super().__init__(f"record size {size} exceeds limit {limit}")


# --------------------------------------------------------------------------- #
# Producer (idempotence + fencing)
# --------------------------------------------------------------------------- #


class ProducerError(StreamingError):
    """Base for producer-side failures."""


class SequenceError(ProducerError):
    """An idempotent producer's per-partition sequence number is out of order.

    Kafka's ``OutOfOrderSequenceException`` analogue: the broker expected
    ``expected`` next but received ``got``. A gap means lost records; a repeat
    (``got < expected``) is a benign duplicate the broker drops.
    """

    def __init__(self, producer_id: str, expected: int, got: int) -> None:
        self.producer_id = producer_id
        self.expected = expected
        self.got = got
        super().__init__(
            f"producer {producer_id!r} sequence out of order: expected {expected}, got {got}"
        )


class FencedProducerError(ProducerError):
    """A producer was fenced: a newer epoch for its transactional id exists.

    Kafka's ``ProducerFencedException`` analogue. Zombie producers (e.g. a
    partitioned-off instance whose replacement re-registered the same
    ``transactional_id``) are fenced so they cannot write or commit.
    """

    def __init__(self, producer_id: str, epoch: int, current_epoch: int) -> None:
        self.producer_id = producer_id
        self.epoch = epoch
        self.current_epoch = current_epoch
        super().__init__(
            f"producer {producer_id!r} fenced: epoch {epoch} < current {current_epoch}"
        )


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


class TransactionError(StreamingError):
    """Base for transactional-producer failures."""


class IllegalStateError(TransactionError):
    """A transactional operation was called in the wrong state.

    e.g. ``commit`` without ``begin``, or ``send`` after ``commit`` without a
    new ``begin``. Carries the offending state machine transition for tests.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Consumer groups
# --------------------------------------------------------------------------- #


class UnknownMemberError(StreamingError):
    """A heartbeat/commit referenced a member id the coordinator does not know."""

    def __init__(self, group_id: str, member_id: str) -> None:
        self.group_id = group_id
        self.member_id = member_id
        super().__init__(f"group {group_id!r} has no member {member_id!r}")


class RebalanceInProgressError(StreamingError):
    """An operation was rejected because the group is mid-rebalance.

    The member must rejoin (``join_group``) to obtain its new assignment before
    fetching/committing again — Kafka's ``RebalanceInProgressException``.
    """

    def __init__(self, group_id: str) -> None:
        self.group_id = group_id
        super().__init__(f"group {group_id!r} is rebalancing; rejoin required")


class CommitConflictError(StreamingError):
    """An offset commit lost a race / used a stale generation.

    Raised when a member commits with a generation older than the group's
    current generation (its assignment was revoked) — Kafka's
    ``CommitFailedException``.
    """

    def __init__(self, group_id: str, member_generation: int, current_generation: int) -> None:
        self.group_id = group_id
        self.member_generation = member_generation
        self.current_generation = current_generation
        super().__init__(
            f"commit for group {group_id!r} rejected: generation "
            f"{member_generation} < current {current_generation}"
        )
