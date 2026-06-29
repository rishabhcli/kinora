"""The in-memory :class:`Broker` — partition logs + groups + EOS, no infra.

State, per broker instance:

* ``_logs`` — a :class:`~app.streaming.log.partition.PartitionLog` per
  ``(topic, partition)``, guarded by a per-partition ``asyncio.Lock`` so
  concurrent produces to one partition are serialised (offsets stay monotonic)
  while different partitions proceed in parallel.
* ``_coordinator`` — one embedded :class:`~app.streaming.log.group.coordinator.
  GroupCoordinator` backing all the group + offset-store methods.
* ``_sequences`` — per ``(producer_id, topic, partition)`` next-expected sequence
  for idempotence; ``_epochs`` — current epoch per producer id for fencing.
* ``_txns`` — open transactions buffering records until commit (exactly-once).

The idempotence + transaction enforcement lives here (the broker is the only
component that sees every append), exactly as Kafka puts it in the broker rather
than the client. The high-level producer/consumer just *use* this surface.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.streaming.log.broker import (
    Broker,
    GroupDescription,
    JoinResult,
    ProduceContext,
)
from app.streaming.log.errors import (
    FencedProducerError,
    IllegalStateError,
    PartitionNotFoundError,
    SequenceError,
    TopicExistsError,
    TopicNotFoundError,
)
from app.streaming.log.group.coordinator import GroupCoordinator
from app.streaming.log.metrics import MetricsSink, NullMetrics
from app.streaming.log.partition import FetchResult, PartitionLog
from app.streaming.log.record import RecordMetadata, TopicPartition, now_ms
from app.streaming.log.topic import TopicConfig

__all__ = ["InMemoryBroker"]


@dataclass(slots=True)
class _BufferedAppend:
    """A transactional record held until its transaction commits."""

    topic: str
    partition: int
    key: bytes | None
    value: bytes | None
    timestamp_ms: int
    headers: tuple[tuple[str, bytes], ...]


@dataclass(slots=True)
class _Transaction:
    """An open transaction's buffered appends + offset commits."""

    producer_id: str
    epoch: int
    appends: list[_BufferedAppend] = field(default_factory=list)
    offset_commits: list[tuple[str, dict[TopicPartition, int]]] = field(default_factory=list)


class InMemoryBroker(Broker):
    """A complete partitioned-log broker that lives entirely in process memory."""

    def __init__(
        self,
        *,
        session_timeout_s: float = 30.0,
        default_assignment_protocol: str = "range",
        metrics: MetricsSink | None = None,
    ) -> None:
        self._configs: dict[str, TopicConfig] = {}
        self._logs: dict[TopicPartition, PartitionLog] = {}
        self._locks: dict[TopicPartition, asyncio.Lock] = {}
        self._admin_lock = asyncio.Lock()
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._coordinator = GroupCoordinator(
            partition_counts=self._partition_count,
            session_timeout_s=session_timeout_s,
            default_protocol=default_assignment_protocol,
        )
        self._sequences: dict[tuple[str, str, int], int] = {}
        self._epochs: dict[str, int] = {}
        self._txns: dict[str, _Transaction] = {}
        self._started = False

    # --- lifecycle ------------------------------------------------------- #

    async def start(self) -> None:
        self._started = True

    async def close(self) -> None:
        self._started = False

    # --- internal helpers ------------------------------------------------ #

    def _partition_count(self, topic: str) -> int:
        config = self._configs.get(topic)
        return config.partitions if config else 0

    def _log(self, topic: str, partition: int) -> PartitionLog:
        config = self._configs.get(topic)
        if config is None:
            raise TopicNotFoundError(topic)
        if partition < 0 or partition >= config.partitions:
            raise PartitionNotFoundError(topic, partition)
        return self._logs[TopicPartition(topic, partition)]

    def _lock(self, topic: str, partition: int) -> asyncio.Lock:
        return self._locks[TopicPartition(topic, partition)]

    # --- admin ----------------------------------------------------------- #

    async def create_topic(self, config: TopicConfig) -> None:
        async with self._admin_lock:
            if config.name in self._configs:
                raise TopicExistsError(config.name)
            self._configs[config.name] = config
            for p in range(config.partitions):
                tp = TopicPartition(config.name, p)
                self._logs[tp] = PartitionLog(config.name, p, config)
                self._locks[tp] = asyncio.Lock()

    async def delete_topic(self, topic: str) -> None:
        async with self._admin_lock:
            config = self._configs.pop(topic, None)
            if config is None:
                raise TopicNotFoundError(topic)
            for p in range(config.partitions):
                tp = TopicPartition(topic, p)
                self._logs.pop(tp, None)
                self._locks.pop(tp, None)
            self._coordinator.drop_topic(topic)

    async def topics(self) -> tuple[str, ...]:
        return tuple(sorted(self._configs))

    async def describe_topic(self, topic: str) -> TopicConfig:
        config = self._configs.get(topic)
        if config is None:
            raise TopicNotFoundError(topic)
        return config

    async def partitions_for(self, topic: str) -> int:
        config = self._configs.get(topic)
        if config is None:
            raise TopicNotFoundError(topic)
        return config.partitions

    # --- produce --------------------------------------------------------- #

    async def produce(
        self,
        topic: str,
        partition: int,
        *,
        key: bytes | None,
        value: bytes | None,
        timestamp_ms: int | None = None,
        headers: tuple[tuple[str, bytes], ...] = (),
        ctx: ProduceContext = ProduceContext(),
    ) -> RecordMetadata:
        ts = timestamp_ms if timestamp_ms is not None else now_ms()
        # Validate the partition exists before any sequence/txn bookkeeping.
        self._log(topic, partition)

        self._check_fence(ctx)

        if ctx.transactional and ctx.transactional_id is not None:
            return self._buffer_transactional(topic, partition, key, value, ts, headers, ctx)

        async with self._lock(topic, partition):
            duplicate = self._check_sequence(ctx, topic, partition)
            if duplicate is not None:
                self._metrics.incr("records_deduplicated", topic=topic)
                return duplicate
            record = self._log(topic, partition).append(
                key=key, value=value, timestamp_ms=ts, headers=headers
            )
            self._advance_sequence(ctx, topic, partition)
            self._metrics.incr("records_produced", topic=topic)
            return RecordMetadata(
                topic=topic,
                partition=partition,
                offset=record.offset,
                timestamp_ms=record.timestamp_ms,
            )

    def _check_fence(self, ctx: ProduceContext) -> None:
        if ctx.producer_id is None:
            return
        current = self._epochs.get(ctx.producer_id, ctx.epoch)
        if ctx.epoch < current:
            raise FencedProducerError(ctx.producer_id, ctx.epoch, current)
        self._epochs[ctx.producer_id] = ctx.epoch

    def _check_sequence(
        self, ctx: ProduceContext, topic: str, partition: int
    ) -> RecordMetadata | None:
        """Validate idempotent sequence; return the prior metadata on a benign dup."""
        if ctx.producer_id is None or ctx.sequence is None:
            return None
        key = (ctx.producer_id, topic, partition)
        expected = self._sequences.get(key, 0)
        if ctx.sequence == expected:
            return None
        if ctx.sequence < expected:
            # Benign duplicate (retry) — return the already-written record's metadata.
            log = self._log(topic, partition)
            # The duplicated record sits at (log_end - (expected - sequence)).
            offset = log.log_end_offset - (expected - ctx.sequence)
            rec = log.read_one(offset)
            ts = rec.timestamp_ms if rec else now_ms()
            return RecordMetadata(topic=topic, partition=partition, offset=offset, timestamp_ms=ts)
        raise SequenceError(ctx.producer_id, expected, ctx.sequence)

    def _advance_sequence(self, ctx: ProduceContext, topic: str, partition: int) -> None:
        if ctx.producer_id is None or ctx.sequence is None:
            return
        key = (ctx.producer_id, topic, partition)
        self._sequences[key] = ctx.sequence + 1

    # --- transactions ---------------------------------------------------- #

    def _txn(self, txn_id: str) -> _Transaction:
        txn = self._txns.get(txn_id)
        if txn is None:
            raise IllegalStateError(f"no open transaction for {txn_id!r}; call begin first")
        return txn

    def begin_transaction(self, transactional_id: str, producer_id: str, epoch: int) -> None:
        """Open a transaction for ``transactional_id`` (called by the producer)."""
        if transactional_id in self._txns:
            raise IllegalStateError(f"transaction already open for {transactional_id!r}")
        self._epochs[producer_id] = epoch
        self._txns[transactional_id] = _Transaction(producer_id=producer_id, epoch=epoch)

    def _buffer_transactional(
        self,
        topic: str,
        partition: int,
        key: bytes | None,
        value: bytes | None,
        ts: int,
        headers: tuple[tuple[str, bytes], ...],
        ctx: ProduceContext,
    ) -> RecordMetadata:
        assert ctx.transactional_id is not None
        txn = self._txn(ctx.transactional_id)
        txn.appends.append(
            _BufferedAppend(
                topic=topic,
                partition=partition,
                key=key,
                value=value,
                timestamp_ms=ts,
                headers=headers,
            )
        )
        # Offset is provisional until commit; transactional sends don't get a
        # durable offset back (Kafka returns it asynchronously post-commit).
        return RecordMetadata(topic=topic, partition=partition, offset=-1, timestamp_ms=ts)

    def stage_offsets_in_transaction(
        self, transactional_id: str, group_id: str, offsets: dict[TopicPartition, int]
    ) -> None:
        """Add a consumer-group offset commit to the open transaction (EOS read-process-write)."""
        self._txn(transactional_id).offset_commits.append((group_id, dict(offsets)))

    async def commit_transaction(self, transactional_id: str) -> list[RecordMetadata]:
        """Atomically flush a transaction's buffered appends + offset commits."""
        txn = self._txn(transactional_id)
        metas: list[RecordMetadata] = []
        # Apply appends in order, serialising per partition.
        for buffered in txn.appends:
            async with self._lock(buffered.topic, buffered.partition):
                record = self._log(buffered.topic, buffered.partition).append(
                    key=buffered.key,
                    value=buffered.value,
                    timestamp_ms=buffered.timestamp_ms,
                    headers=buffered.headers,
                )
                metas.append(
                    RecordMetadata(
                        topic=buffered.topic,
                        partition=buffered.partition,
                        offset=record.offset,
                        timestamp_ms=record.timestamp_ms,
                    )
                )
        for group_id, offsets in txn.offset_commits:
            self._coordinator.commit(group_id, offsets)
        del self._txns[transactional_id]
        return metas

    async def abort_transaction(self, transactional_id: str) -> None:
        """Discard a transaction's buffered records + offset commits."""
        self._txns.pop(transactional_id, None)

    # --- consume --------------------------------------------------------- #

    async def fetch(
        self,
        topic: str,
        partition: int,
        offset: int,
        *,
        max_records: int = 500,
        max_bytes: int | None = None,
    ) -> FetchResult:
        async with self._lock(topic, partition):
            result = self._log(topic, partition).fetch(
                offset, max_records=max_records, max_bytes=max_bytes
            )
        self._metrics.incr("fetch_requests", topic=topic)
        self._metrics.incr("records_fetched", len(result.records), topic=topic)
        self._metrics.observe("fetch_batch_size", len(result.records), topic=topic)
        return result

    async def beginning_offsets(
        self, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int]:
        return {tp: self._log(tp.topic, tp.partition).log_start_offset for tp in partitions}

    async def end_offsets(
        self, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int]:
        return {tp: self._log(tp.topic, tp.partition).log_end_offset for tp in partitions}

    async def offsets_for_times(
        self, timestamps: dict[TopicPartition, int]
    ) -> dict[TopicPartition, int | None]:
        return {
            tp: self._log(tp.topic, tp.partition).offset_for_timestamp(ts)
            for tp, ts in timestamps.items()
        }

    # --- maintenance ----------------------------------------------------- #

    async def maintain(self, *, now: int | None = None) -> int:
        """Run retention + compaction across every partition; return records removed."""
        when = now if now is not None else now_ms()
        removed = 0
        for tp, log in list(self._logs.items()):
            async with self._lock(tp.topic, tp.partition):
                count = log.maintain(when)
            if count:
                self._metrics.incr("records_cleaned", count, topic=tp.topic)
            removed += count
        return removed

    # --- consumer-group offset store ------------------------------------ #

    async def commit_offsets(
        self,
        group_id: str,
        offsets: dict[TopicPartition, int],
        *,
        generation: int | None = None,
        member_id: str | None = None,
    ) -> None:
        self._coordinator.commit(
            group_id, offsets, generation=generation, member_id=member_id
        )
        self._metrics.incr("offset_commits", group=group_id)

    async def fetch_committed(
        self, group_id: str, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int | None]:
        return self._coordinator.fetch_committed(group_id, partitions)

    async def list_committed(self, group_id: str) -> dict[TopicPartition, int]:
        return self._coordinator.list_committed(group_id)

    # --- consumer-group membership -------------------------------------- #

    async def join_group(
        self,
        group_id: str,
        *,
        member_id: str | None,
        subscription: tuple[str, ...],
        protocol: str = "range",
    ) -> JoinResult:
        result = self._coordinator.join(
            group_id, member_id=member_id, subscription=subscription, protocol=protocol
        )
        self._metrics.incr("rebalances", group=group_id)
        return result

    async def leave_group(self, group_id: str, member_id: str) -> None:
        self._coordinator.leave(group_id, member_id)

    async def heartbeat(self, group_id: str, member_id: str, generation: int) -> bool:
        return self._coordinator.heartbeat(group_id, member_id, generation)

    async def describe_group(self, group_id: str) -> GroupDescription:
        return self._coordinator.describe(group_id)
