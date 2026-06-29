"""Topic + retention/compaction configuration.

A *topic* is a named, partitioned stream. Its :class:`TopicConfig` captures the
durable policy a broker applies to every partition:

* **partitions** — fixed at creation (the unit of parallelism + key ordering).
* **cleanup policy** — ``DELETE`` (time/size-bounded log), ``COMPACT`` (keep the
  latest value per key, plus tombstone handling), or both (compact, then also
  age out tail segments). Kafka's ``cleanup.policy``.
* **retention** — for the delete policy: by age (``retention_ms``) and/or by
  total bytes (``retention_bytes``); ``-1`` disables a dimension.
* **compaction tuning** — ``min_compaction_lag_ms`` (don't compact records
  younger than this) and ``delete_retention_ms`` (how long tombstones linger so
  downstream consumers observe the delete before it's reaped).
* **max_message_bytes** — per-record size ceiling enforced at append.

:class:`RetentionPolicy` is a small pure object the partition's cleaner consults;
it never holds time itself — the caller passes ``now_ms`` so retention is fully
deterministic in tests.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field, replace

from app.streaming.log.errors import InvalidConfigError

__all__ = [
    "CleanupPolicy",
    "RetentionPolicy",
    "TopicConfig",
]

#: Sentinel meaning "no limit" for a retention dimension (Kafka uses ``-1``).
UNLIMITED = -1


class CleanupPolicy(enum.Flag):
    """How a partition's log is cleaned. Composable as a flag (``COMPACT | DELETE``)."""

    DELETE = enum.auto()
    COMPACT = enum.auto()

    @property
    def compacts(self) -> bool:
        """Whether compaction (keep-latest-per-key) is enabled."""
        return bool(self & CleanupPolicy.COMPACT)

    @property
    def deletes(self) -> bool:
        """Whether age/size-based deletion is enabled."""
        return bool(self & CleanupPolicy.DELETE)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Time- and size-bounded retention for the ``DELETE`` cleanup policy.

    ``retention_ms``/``retention_bytes`` of :data:`UNLIMITED` disable that
    dimension. ``segment_bytes`` is the target segment roll size used to decide
    *whole-segment* eviction (Kafka deletes whole segments, never partial ones).
    """

    retention_ms: int = 7 * 24 * 60 * 60 * 1000  # 7 days
    retention_bytes: int = UNLIMITED
    segment_bytes: int = 1 << 20  # 1 MiB

    def __post_init__(self) -> None:
        if self.retention_ms != UNLIMITED and self.retention_ms < 0:
            raise InvalidConfigError("retention_ms must be >= 0 or UNLIMITED (-1)")
        if self.retention_bytes != UNLIMITED and self.retention_bytes < 0:
            raise InvalidConfigError("retention_bytes must be >= 0 or UNLIMITED (-1)")
        if self.segment_bytes <= 0:
            raise InvalidConfigError("segment_bytes must be positive")

    def is_expired(self, record_ts_ms: int, now_ms: int) -> bool:
        """Whether a record at ``record_ts_ms`` is older than ``retention_ms``."""
        if self.retention_ms == UNLIMITED:
            return False
        return (now_ms - record_ts_ms) > self.retention_ms

    @property
    def size_bounded(self) -> bool:
        """Whether a byte ceiling is configured."""
        return self.retention_bytes != UNLIMITED


@dataclass(frozen=True, slots=True)
class TopicConfig:
    """Durable per-topic configuration applied uniformly across its partitions."""

    name: str
    partitions: int = 1
    cleanup_policy: CleanupPolicy = CleanupPolicy.DELETE
    retention: RetentionPolicy = field(default_factory=RetentionPolicy)
    min_compaction_lag_ms: int = 0
    delete_retention_ms: int = 24 * 60 * 60 * 1000  # 1 day tombstone grace
    max_message_bytes: int = 1 << 20  # 1 MiB

    def __post_init__(self) -> None:
        if not self.name:
            raise InvalidConfigError("topic name must be non-empty")
        if self.partitions <= 0:
            raise InvalidConfigError("topic must have at least one partition")
        if self.min_compaction_lag_ms < 0:
            raise InvalidConfigError("min_compaction_lag_ms must be >= 0")
        if self.delete_retention_ms < 0:
            raise InvalidConfigError("delete_retention_ms must be >= 0")
        if self.max_message_bytes <= 0:
            raise InvalidConfigError("max_message_bytes must be positive")

    # --- ergonomic constructors ----------------------------------------- #

    @classmethod
    def deleted(
        cls,
        name: str,
        *,
        partitions: int = 1,
        retention_ms: int = 7 * 24 * 60 * 60 * 1000,
        retention_bytes: int = UNLIMITED,
        max_message_bytes: int = 1 << 20,
    ) -> TopicConfig:
        """A standard delete-policy (time/size bounded) topic."""
        return cls(
            name=name,
            partitions=partitions,
            cleanup_policy=CleanupPolicy.DELETE,
            retention=RetentionPolicy(
                retention_ms=retention_ms, retention_bytes=retention_bytes
            ),
            max_message_bytes=max_message_bytes,
        )

    @classmethod
    def compacted(
        cls,
        name: str,
        *,
        partitions: int = 1,
        also_delete: bool = False,
        delete_retention_ms: int = 24 * 60 * 60 * 1000,
        min_compaction_lag_ms: int = 0,
        max_message_bytes: int = 1 << 20,
    ) -> TopicConfig:
        """A compacted (keep-latest-per-key) topic, optionally also age-bounded."""
        policy = CleanupPolicy.COMPACT
        if also_delete:
            policy |= CleanupPolicy.DELETE
        return cls(
            name=name,
            partitions=partitions,
            cleanup_policy=policy,
            delete_retention_ms=delete_retention_ms,
            min_compaction_lag_ms=min_compaction_lag_ms,
            max_message_bytes=max_message_bytes,
        )

    def with_partitions(self, partitions: int) -> TopicConfig:
        """A copy with a different partition count (used by broker create paths)."""
        return replace(self, partitions=partitions)
