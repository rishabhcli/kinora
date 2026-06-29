"""The high-level producer — partitioning, batching, idempotence, transactions.

A :class:`Producer` wraps any :class:`~app.streaming.log.broker.Broker` and adds
the client-side concerns Kafka producers own:

* **Partitioning** — resolves a :class:`~app.streaming.log.record.ProducerRecord`'s
  target partition via the configured :class:`~app.streaming.log.partitioner.
  Partitioner` (keyed → murmur2, keyless → sticky) unless the record names one.
* **Batching** — :meth:`send` enqueues into a per-partition batch; :meth:`flush`
  (or reaching ``batch_size`` / ``linger_ms``) drains batches to the broker. Each
  ``send`` returns a future that resolves to the record's
  :class:`~app.streaming.log.record.RecordMetadata` once flushed.
* **Idempotence** — when ``enable_idempotence`` the producer is assigned a stable
  ``producer_id`` and tags every append with a per-partition monotonic sequence,
  so a retried send is de-duplicated by the broker (no double-append).
* **Transactions** — ``transactional_id`` upgrades the producer to exactly-once:
  :meth:`begin_transaction` / :meth:`send_offsets_to_transaction` /
  :meth:`commit_transaction` / :meth:`abort_transaction` make a set of appends
  (and the consumer offsets that produced them) atomic. ``transaction()`` is an
  async context manager that commits on success and aborts on exception.

The producer is safe to share across tasks; its batch buffers are guarded by an
``asyncio.Lock``. It targets the broker protocol only, so it is broker-agnostic.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from app.streaming.log.broker import Broker, ProduceContext
from app.streaming.log.errors import IllegalStateError
from app.streaming.log.partitioner import DefaultPartitioner, Partitioner
from app.streaming.log.record import (
    ProducerRecord,
    RecordMetadata,
    TopicPartition,
    now_ms,
)

__all__ = ["Producer", "ProducerConfig"]


@dataclass(slots=True)
class ProducerConfig:
    """Producer tuning. Defaults are safe (idempotent, modest batching)."""

    enable_idempotence: bool = True
    transactional_id: str | None = None
    batch_size: int = 100
    linger_ms: int = 0
    max_in_flight: int = 5

    def __post_init__(self) -> None:
        if self.transactional_id is not None and not self.enable_idempotence:
            # Transactions require idempotence (Kafka enforces the same).
            self.enable_idempotence = True


@dataclass(slots=True)
class _Pending:
    """One buffered record + the future that resolves to its metadata."""

    record: ProducerRecord
    partition: int
    future: asyncio.Future[RecordMetadata]


@dataclass(slots=True)
class _PartitionState:
    """Per-partition idempotence sequence + buffered batch."""

    next_sequence: int = 0
    batch: list[_Pending] = field(default_factory=list)


class Producer:
    """A batched, idempotent, optionally-transactional log producer."""

    def __init__(
        self,
        broker: Broker,
        *,
        config: ProducerConfig | None = None,
        partitioner: Partitioner | None = None,
    ) -> None:
        self._broker = broker
        self._config = config or ProducerConfig()
        self._partitioner = partitioner or DefaultPartitioner()
        self._partitions: dict[TopicPartition, _PartitionState] = {}
        self._lock = asyncio.Lock()
        self._producer_id: str | None = None
        self._epoch = 0
        self._in_transaction = False
        self._closed = False
        if self._config.enable_idempotence:
            self._producer_id = f"pid-{uuid.uuid4().hex[:16]}"

    @property
    def producer_id(self) -> str | None:
        """The stable idempotence id (``None`` if idempotence is disabled)."""
        return self._producer_id

    @property
    def epoch(self) -> int:
        """The current producer epoch (bumped on ``init_transactions`` fencing)."""
        return self._epoch

    def _pstate(self, tp: TopicPartition) -> _PartitionState:
        state = self._partitions.get(tp)
        if state is None:
            state = _PartitionState()
            self._partitions[tp] = state
        return state

    async def _resolve_partition(self, record: ProducerRecord) -> int:
        if record.partition is not None:
            return record.partition
        n = await self._broker.partitions_for(record.topic)
        return self._partitioner.partition(record.key, n)

    # --- send / flush ---------------------------------------------------- #

    async def send(self, record: ProducerRecord) -> asyncio.Future[RecordMetadata]:
        """Buffer ``record`` for its resolved partition; return its result future.

        Awaiting the returned future yields the durable
        :class:`~app.streaming.log.record.RecordMetadata` once the batch flushes
        (either at ``batch_size``/``linger_ms`` or on an explicit :meth:`flush`).
        """
        if self._closed:
            raise IllegalStateError("producer is closed")
        partition = await self._resolve_partition(record)
        tp = TopicPartition(record.topic, partition)
        loop = asyncio.get_event_loop()
        future: asyncio.Future[RecordMetadata] = loop.create_future()
        async with self._lock:
            state = self._pstate(tp)
            state.batch.append(_Pending(record=record, partition=partition, future=future))
            should_flush = (
                len(state.batch) >= self._config.batch_size or self._config.linger_ms == 0
            )
        if should_flush:
            await self._flush_partition(tp)
        return future

    async def send_and_wait(self, record: ProducerRecord) -> RecordMetadata:
        """Convenience: :meth:`send` then await the result in one call."""
        future = await self.send(record)
        return await future

    async def flush(self) -> None:
        """Drain every buffered partition batch to the broker."""
        async with self._lock:
            partitions = list(self._partitions)
        for tp in partitions:
            await self._flush_partition(tp)

    async def _flush_partition(self, tp: TopicPartition) -> None:
        async with self._lock:
            state = self._partitions.get(tp)
            if state is None or not state.batch:
                return
            pending = state.batch
            state.batch = []
        for item in pending:
            await self._dispatch(tp, item, state)

    async def _dispatch(self, tp: TopicPartition, item: _Pending, state: _PartitionState) -> None:
        ctx = self._produce_context(tp, state)
        try:
            meta = await self._broker.produce(
                tp.topic,
                tp.partition,
                key=item.record.key,
                value=item.record.value,
                timestamp_ms=item.record.timestamp_ms or now_ms(),
                headers=item.record.headers,
                ctx=ctx,
            )
        except Exception as exc:  # noqa: BLE001 - propagated to the awaiting caller
            if not item.future.done():
                item.future.set_exception(exc)
            return
        if ctx.sequence is not None:
            state.next_sequence = ctx.sequence + 1
        if not item.future.done():
            item.future.set_result(meta)

    def _produce_context(self, tp: TopicPartition, state: _PartitionState) -> ProduceContext:
        if not self._config.enable_idempotence:
            return ProduceContext()
        return ProduceContext(
            producer_id=self._producer_id,
            epoch=self._epoch,
            sequence=state.next_sequence,
            transactional=self._in_transaction,
            transactional_id=self._config.transactional_id if self._in_transaction else None,
        )

    # --- transactions ---------------------------------------------------- #

    async def init_transactions(self) -> None:
        """Register the transactional id and bump the epoch (fences zombies)."""
        if self._config.transactional_id is None:
            raise IllegalStateError("producer has no transactional_id")
        self._epoch += 1

    async def begin_transaction(self) -> None:
        """Open a transaction. Subsequent sends are buffered until commit/abort."""
        if self._config.transactional_id is None:
            raise IllegalStateError("producer has no transactional_id")
        if self._in_transaction:
            raise IllegalStateError("transaction already in progress")
        begin = getattr(self._broker, "begin_transaction", None)
        if begin is None:
            raise IllegalStateError("broker does not support transactions")
        assert self._producer_id is not None
        begin(self._config.transactional_id, self._producer_id, self._epoch)
        self._in_transaction = True

    async def send_offsets_to_transaction(
        self, offsets: dict[TopicPartition, int], group_id: str
    ) -> None:
        """Include a consumer group's offset commit in the open transaction (EOS)."""
        if not self._in_transaction:
            raise IllegalStateError("no transaction in progress")
        stage = getattr(self._broker, "stage_offsets_in_transaction", None)
        if stage is None:
            raise IllegalStateError("broker does not support transactional offsets")
        await self.flush()
        assert self._config.transactional_id is not None
        stage(self._config.transactional_id, group_id, offsets)

    async def commit_transaction(self) -> list[RecordMetadata]:
        """Flush + atomically commit the open transaction's appends and offsets."""
        if not self._in_transaction:
            raise IllegalStateError("no transaction in progress")
        await self.flush()
        commit = getattr(self._broker, "commit_transaction", None)
        if commit is None:
            raise IllegalStateError("broker does not support transactions")
        assert self._config.transactional_id is not None
        result: list[RecordMetadata] = await commit(self._config.transactional_id)
        self._in_transaction = False
        return result

    async def abort_transaction(self) -> None:
        """Discard the open transaction's buffered appends and offsets."""
        if not self._in_transaction:
            raise IllegalStateError("no transaction in progress")
        abort = getattr(self._broker, "abort_transaction", None)
        if abort is not None:
            assert self._config.transactional_id is not None
            await abort(self._config.transactional_id)
        # Drop any still-buffered (un-dispatched) records for this txn.
        async with self._lock:
            for state in self._partitions.values():
                for item in state.batch:
                    if not item.future.done():
                        item.future.cancel()
                state.batch = []
        self._in_transaction = False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Producer]:
        """Scope a transaction: commit on clean exit, abort on exception."""
        await self.begin_transaction()
        try:
            yield self
        except BaseException:
            await self.abort_transaction()
            raise
        else:
            await self.commit_transaction()

    # --- lifecycle ------------------------------------------------------- #

    async def close(self) -> None:
        """Flush outstanding batches and mark the producer closed."""
        if self._closed:
            return
        await self.flush()
        self._closed = True
