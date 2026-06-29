"""The Redis-Streams-backed :class:`Broker`.

Each ``(topic, partition)`` is a Redis Stream keyed
``{ns}:p:{topic}:{partition}``. A record is one stream entry carrying the
broker-assigned dense ``offset`` (from a per-partition ``INCR`` counter), the
key/value (base64 so binary is safe through ``decode_responses=True``), the
timestamp, and headers. The dense offset — not the Redis entry id — is the
addressing unit, so offset semantics match the in-memory broker exactly.

Durable Redis state:

* ``{ns}:topics`` (set) + ``{ns}:cfg:{topic}`` (hash) — the topic registry/config.
* ``{ns}:p:{topic}:{p}`` (stream) — the partition log.
* ``{ns}:end:{topic}:{p}`` (counter) — next offset to assign.
* ``{ns}:start:{topic}:{p}`` (string) — log-start offset (advances on retention).
* ``{ns}:seq:{producer}:{topic}:{p}`` (string) — idempotence next-sequence.
* ``{ns}:committed:{group}`` (hash ``"topic/partition" → offset``) — group offsets.

Consumer-*group membership* + rebalancing is in-process (the embedded
:class:`~app.streaming.log.group.coordinator.GroupCoordinator`); only the durable
*offset* state lives in Redis. That matches how a single API/worker process owns
its consumers while their progress survives restarts — and keeps this facet free
of a separate group-coordination service. Idempotence + transactions are enforced
here, the same place the in-memory broker enforces them.
"""

from __future__ import annotations

import base64
import json

from app.streaming.log.broker import (
    Broker,
    GroupDescription,
    JoinResult,
    ProduceContext,
)
from app.streaming.log.errors import (
    FencedProducerError,
    IllegalStateError,
    OffsetOutOfRangeError,
    PartitionNotFoundError,
    SequenceError,
    TopicExistsError,
    TopicNotFoundError,
)
from app.streaming.log.group.coordinator import GroupCoordinator
from app.streaming.log.metrics import MetricsSink, NullMetrics
from app.streaming.log.partition import FetchResult
from app.streaming.log.record import ConsumerRecord, RecordMetadata, TopicPartition, now_ms
from app.streaming.log.redis.client import StreamRedis
from app.streaming.log.topic import CleanupPolicy, RetentionPolicy, TopicConfig

__all__ = ["RedisStreamsBroker"]


def _b64(data: bytes | None) -> str:
    return "" if data is None else base64.b64encode(data).decode("ascii")


def _unb64(text: str, present: str) -> bytes | None:
    return base64.b64decode(text) if present == "1" else None


class RedisStreamsBroker(Broker):
    """A partitioned-log broker backed by Redis Streams."""

    def __init__(
        self,
        redis: StreamRedis,
        *,
        namespace: str = "kinora:stream",
        session_timeout_s: float = 30.0,
        default_assignment_protocol: str = "range",
        metrics: MetricsSink | None = None,
    ) -> None:
        self._r = redis
        self._ns = namespace
        self._configs: dict[str, TopicConfig] = {}
        self._txns: dict[str, list[tuple]] = {}
        self._txn_epochs: dict[str, tuple[str, int]] = {}
        self._epochs: dict[str, int] = {}
        self._metrics: MetricsSink = metrics or NullMetrics()
        self._coordinator = GroupCoordinator(
            partition_counts=self._cached_partition_count,
            session_timeout_s=session_timeout_s,
            default_protocol=default_assignment_protocol,
        )
        self._started = False

    def _cached_partition_count(self, topic: str) -> int:
        """Partition count from the in-memory config cache (0 if unknown)."""
        config = self._configs.get(topic)
        return config.partitions if config is not None else 0

    # --- key helpers ----------------------------------------------------- #

    def _k_topics(self) -> str:
        return f"{self._ns}:topics"

    def _k_cfg(self, topic: str) -> str:
        return f"{self._ns}:cfg:{topic}"

    def _k_stream(self, topic: str, p: int) -> str:
        return f"{self._ns}:p:{topic}:{p}"

    def _k_end(self, topic: str, p: int) -> str:
        return f"{self._ns}:end:{topic}:{p}"

    def _k_start(self, topic: str, p: int) -> str:
        return f"{self._ns}:start:{topic}:{p}"

    def _k_seq(self, producer: str, topic: str, p: int) -> str:
        return f"{self._ns}:seq:{producer}:{topic}:{p}"

    def _k_committed(self, group: str) -> str:
        return f"{self._ns}:committed:{group}"

    # --- lifecycle ------------------------------------------------------- #

    async def start(self) -> None:
        """Hydrate the in-memory topic-config cache from Redis (idempotent)."""
        if self._started:
            return
        for topic in await self._r.smembers(self._k_topics()):
            cfg = await self._load_config(topic)
            if cfg is not None:
                self._configs[topic] = cfg
        self._started = True

    async def close(self) -> None:
        self._started = False

    # --- admin ----------------------------------------------------------- #

    def _encode_config(self, config: TopicConfig) -> dict[str, str]:
        return {
            "name": config.name,
            "partitions": str(config.partitions),
            "cleanup_policy": str(config.cleanup_policy.value),
            "retention_ms": str(config.retention.retention_ms),
            "retention_bytes": str(config.retention.retention_bytes),
            "segment_bytes": str(config.retention.segment_bytes),
            "min_compaction_lag_ms": str(config.min_compaction_lag_ms),
            "delete_retention_ms": str(config.delete_retention_ms),
            "max_message_bytes": str(config.max_message_bytes),
        }

    async def _load_config(self, topic: str) -> TopicConfig | None:
        raw = await self._r.hgetall(self._k_cfg(topic))
        if not raw:
            return None
        return TopicConfig(
            name=raw["name"],
            partitions=int(raw["partitions"]),
            cleanup_policy=CleanupPolicy(int(raw["cleanup_policy"])),
            retention=RetentionPolicy(
                retention_ms=int(raw["retention_ms"]),
                retention_bytes=int(raw["retention_bytes"]),
                segment_bytes=int(raw["segment_bytes"]),
            ),
            min_compaction_lag_ms=int(raw["min_compaction_lag_ms"]),
            delete_retention_ms=int(raw["delete_retention_ms"]),
            max_message_bytes=int(raw["max_message_bytes"]),
        )

    async def create_topic(self, config: TopicConfig) -> None:
        if config.name in self._configs or await self._r.smembers(self._k_topics()) & {config.name}:
            raise TopicExistsError(config.name)
        await self._r.hset(self._k_cfg(config.name), self._encode_config(config))
        await self._r.sadd(self._k_topics(), config.name)
        for p in range(config.partitions):
            await self._r.set(self._k_end(config.name, p), "0")
            await self._r.set(self._k_start(config.name, p), "0")
        self._configs[config.name] = config

    async def delete_topic(self, topic: str) -> None:
        config = self._configs.get(topic) or await self._load_config(topic)
        if config is None:
            raise TopicNotFoundError(topic)
        keys = [self._k_cfg(topic)]
        for p in range(config.partitions):
            keys += [self._k_stream(topic, p), self._k_end(topic, p), self._k_start(topic, p)]
        await self._r.delete(*keys)
        await self._r.srem(self._k_topics(), topic)
        self._configs.pop(topic, None)
        self._coordinator.drop_topic(topic)

    async def topics(self) -> tuple[str, ...]:
        return tuple(sorted(await self._r.smembers(self._k_topics())))

    async def describe_topic(self, topic: str) -> TopicConfig:
        config = self._configs.get(topic) or await self._load_config(topic)
        if config is None:
            raise TopicNotFoundError(topic)
        self._configs[topic] = config
        return config

    async def partitions_for(self, topic: str) -> int:
        return (await self.describe_topic(topic)).partitions

    def _require(self, topic: str, partition: int) -> TopicConfig:
        config = self._configs.get(topic)
        if config is None:
            raise TopicNotFoundError(topic)
        if partition < 0 or partition >= config.partitions:
            raise PartitionNotFoundError(topic, partition)
        return config

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
        self._require(topic, partition)
        ts = timestamp_ms if timestamp_ms is not None else now_ms()
        self._check_fence(ctx)

        if ctx.transactional and ctx.transactional_id is not None:
            buf = self._txns.get(ctx.transactional_id)
            if buf is None:
                raise IllegalStateError(
                    f"no open transaction for {ctx.transactional_id!r}; call begin first"
                )
            buf.append(("append", topic, partition, key, value, ts, headers))
            return RecordMetadata(topic=topic, partition=partition, offset=-1, timestamp_ms=ts)

        dup = await self._check_sequence(ctx, topic, partition)
        if dup is not None:
            self._metrics.incr("records_deduplicated", topic=topic)
            return dup
        offset = await self._append(topic, partition, key, value, ts, headers)
        await self._advance_sequence(ctx, topic, partition)
        self._metrics.incr("records_produced", topic=topic)
        return RecordMetadata(topic=topic, partition=partition, offset=offset, timestamp_ms=ts)

    async def _append(
        self,
        topic: str,
        partition: int,
        key: bytes | None,
        value: bytes | None,
        ts: int,
        headers: tuple[tuple[str, bytes], ...],
    ) -> int:
        offset = (await self._r.incr(self._k_end(topic, partition))) - 1
        fields = {
            "off": str(offset),
            "ts": str(ts),
            "k": _b64(key),
            "kp": "1" if key is not None else "0",
            "v": _b64(value),
            "vp": "1" if value is not None else "0",
            "h": json.dumps([[hk, _b64(hv)] for hk, hv in headers]),
        }
        # Stream id encodes the dense offset (offset+1 so it is >= 1, Redis requires).
        await self._r.xadd(self._k_stream(topic, partition), fields, entry_id=f"{offset + 1}-0")
        return offset

    def _check_fence(self, ctx: ProduceContext) -> None:
        if ctx.producer_id is None:
            return
        current = self._epochs.get(ctx.producer_id, ctx.epoch)
        if ctx.epoch < current:
            raise FencedProducerError(ctx.producer_id, ctx.epoch, current)
        self._epochs[ctx.producer_id] = ctx.epoch

    async def _check_sequence(
        self, ctx: ProduceContext, topic: str, partition: int
    ) -> RecordMetadata | None:
        if ctx.producer_id is None or ctx.sequence is None:
            return None
        seq_key = self._k_seq(ctx.producer_id, topic, partition)
        expected = int(await self._r.get(seq_key) or "0")
        if ctx.sequence == expected:
            return None
        if ctx.sequence < expected:
            end = int(await self._r.get(self._k_end(topic, partition)) or "0")
            offset = end - (expected - ctx.sequence)
            rec = await self._read_one(topic, partition, offset)
            ts = rec.timestamp_ms if rec else now_ms()
            return RecordMetadata(topic=topic, partition=partition, offset=offset, timestamp_ms=ts)
        raise SequenceError(ctx.producer_id, expected, ctx.sequence)

    async def _advance_sequence(self, ctx: ProduceContext, topic: str, partition: int) -> None:
        if ctx.producer_id is None or ctx.sequence is None:
            return
        await self._r.set(self._k_seq(ctx.producer_id, topic, partition), str(ctx.sequence + 1))

    # --- transactions ---------------------------------------------------- #

    def begin_transaction(self, transactional_id: str, producer_id: str, epoch: int) -> None:
        """Open a transaction (producer-facing seam, mirrors the in-memory broker)."""
        if transactional_id in self._txns:
            raise IllegalStateError(f"transaction already open for {transactional_id!r}")
        self._epochs[producer_id] = epoch
        self._txns[transactional_id] = []
        self._txn_epochs[transactional_id] = (producer_id, epoch)

    def stage_offsets_in_transaction(
        self, transactional_id: str, group_id: str, offsets: dict[TopicPartition, int]
    ) -> None:
        buf = self._txns.get(transactional_id)
        if buf is None:
            raise IllegalStateError(f"no open transaction for {transactional_id!r}")
        buf.append(("offsets", group_id, dict(offsets)))

    async def commit_transaction(self, transactional_id: str) -> list[RecordMetadata]:
        buf = self._txns.get(transactional_id)
        if buf is None:
            raise IllegalStateError(f"no open transaction for {transactional_id!r}")
        metas: list[RecordMetadata] = []
        for op in buf:
            if op[0] == "append":
                _, topic, partition, key, value, ts, headers = op
                offset = await self._append(topic, partition, key, value, ts, headers)
                metas.append(
                    RecordMetadata(
                        topic=topic, partition=partition, offset=offset, timestamp_ms=ts
                    )
                )
            else:  # "offsets"
                _, group_id, offsets = op
                await self.commit_offsets(group_id, offsets)
        del self._txns[transactional_id]
        self._txn_epochs.pop(transactional_id, None)
        return metas

    async def abort_transaction(self, transactional_id: str) -> None:
        self._txns.pop(transactional_id, None)
        self._txn_epochs.pop(transactional_id, None)

    # --- consume --------------------------------------------------------- #

    async def _bounds(self, topic: str, partition: int) -> tuple[int, int]:
        end = int(await self._r.get(self._k_end(topic, partition)) or "0")
        start = int(await self._r.get(self._k_start(topic, partition)) or "0")
        return start, end

    async def _read_one(self, topic: str, partition: int, offset: int) -> ConsumerRecord | None:
        entries = await self._r.xrange(
            self._k_stream(topic, partition),
            start=f"{offset + 1}-0",
            end=f"{offset + 1}-0",
            count=1,
        )
        if not entries:
            return None
        return self._decode(topic, partition, entries[0][1])

    def _decode(self, topic: str, partition: int, fields: dict[str, str]) -> ConsumerRecord:
        headers = tuple(
            (hk, base64.b64decode(hv)) for hk, hv in json.loads(fields.get("h", "[]"))
        )
        return ConsumerRecord(
            topic=topic,
            partition=partition,
            offset=int(fields["off"]),
            timestamp_ms=int(fields["ts"]),
            key=_unb64(fields["k"], fields["kp"]),
            value=_unb64(fields["v"], fields["vp"]),
            headers=headers,
        )

    async def fetch(
        self,
        topic: str,
        partition: int,
        offset: int,
        *,
        max_records: int = 500,
        max_bytes: int | None = None,
    ) -> FetchResult:
        self._require(topic, partition)
        start, end = await self._bounds(topic, partition)
        if offset < start or offset > end:
            raise OffsetOutOfRangeError(topic, partition, offset, start, end)
        if offset == end:
            return FetchResult(
                records=(), next_offset=offset, high_watermark=end, log_start_offset=start
            )
        entries = await self._r.xrange(
            self._k_stream(topic, partition),
            start=f"{offset + 1}-0",
            end="+",
            count=max_records,
        )
        records: list[ConsumerRecord] = []
        accumulated = 0
        next_offset = offset
        for _eid, fields in entries:
            rec = self._decode(topic, partition, fields)
            size = len(rec.key or b"") + len(rec.value or b"")
            if max_bytes is not None and records and accumulated + size > max_bytes:
                break
            records.append(rec)
            accumulated += size
            next_offset = rec.offset + 1
        self._metrics.incr("fetch_requests", topic=topic)
        self._metrics.incr("records_fetched", len(records), topic=topic)
        self._metrics.observe("fetch_batch_size", len(records), topic=topic)
        return FetchResult(
            records=tuple(records),
            next_offset=next_offset if records else offset,
            high_watermark=end,
            log_start_offset=start,
        )

    async def beginning_offsets(
        self, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int]:
        out: dict[TopicPartition, int] = {}
        for tp in partitions:
            start, _ = await self._bounds(tp.topic, tp.partition)
            out[tp] = start
        return out

    async def end_offsets(
        self, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int]:
        out: dict[TopicPartition, int] = {}
        for tp in partitions:
            _, end = await self._bounds(tp.topic, tp.partition)
            out[tp] = end
        return out

    async def offsets_for_times(
        self, timestamps: dict[TopicPartition, int]
    ) -> dict[TopicPartition, int | None]:
        out: dict[TopicPartition, int | None] = {}
        for tp, ts in timestamps.items():
            entries = await self._r.xrange(self._k_stream(tp.topic, tp.partition))
            found: int | None = None
            for _eid, fields in entries:
                if int(fields["ts"]) >= ts:
                    found = int(fields["off"])
                    break
            out[tp] = found
        return out

    # --- retention / compaction ----------------------------------------- #

    async def maintain(self, *, now: int | None = None) -> int:
        """Apply retention (xtrim) + compaction across every partition; return removed."""
        when = now if now is not None else now_ms()
        removed = 0
        for topic, config in list(self._configs.items()):
            count = 0
            for p in range(config.partitions):
                count += await self._maintain_partition(topic, p, config, when)
            if count:
                self._metrics.incr("records_cleaned", count, topic=topic)
            removed += count
        return removed

    async def _maintain_partition(
        self, topic: str, p: int, config: TopicConfig, when: int
    ) -> int:
        removed = 0
        if config.cleanup_policy.compacts:
            removed += await self._compact_partition(topic, p, config, when)
        if config.cleanup_policy.deletes:
            removed += await self._retain_partition(topic, p, config, when)
        return removed

    async def _retain_partition(
        self, topic: str, p: int, config: TopicConfig, when: int
    ) -> int:
        entries = await self._r.xrange(self._k_stream(topic, p))
        if len(entries) <= 1:
            return 0
        retention = config.retention
        cutoff_offset: int | None = None
        for _eid, fields in entries[:-1]:  # never evict the newest record
            if retention.is_expired(int(fields["ts"]), when):
                cutoff_offset = int(fields["off"])
            else:
                break
        if cutoff_offset is None:
            return 0
        new_start = cutoff_offset + 1
        removed = await self._r.xtrim_minid(self._k_stream(topic, p), f"{new_start + 1}-0")
        await self._r.set(self._k_start(topic, p), str(new_start))
        return removed

    async def _compact_partition(
        self, topic: str, p: int, config: TopicConfig, when: int
    ) -> int:
        entries = await self._r.xrange(self._k_stream(topic, p))
        if not entries:
            return 0
        lag = config.min_compaction_lag_ms
        compactible = entries
        tail: list = []
        if lag > 0:
            for idx, (_eid, fields) in enumerate(entries):
                if (when - int(fields["ts"])) < lag:
                    compactible, tail = entries[:idx], entries[idx:]
                    break
        last_idx: dict[str, int] = {}
        for idx, (_eid, fields) in enumerate(compactible):
            if fields["kp"] == "1":
                last_idx[fields["k"]] = idx
        survivors: list = []
        for idx, (eid, fields) in enumerate(compactible):
            if fields["kp"] != "1":
                survivors.append((eid, fields))
                continue
            if last_idx.get(fields["k"]) != idx:
                continue
            if fields["vp"] == "0" and (when - int(fields["ts"])) >= config.delete_retention_ms:
                continue
            survivors.append((eid, fields))
        survivors.extend(tail)
        removed = len(entries) - len(survivors)
        if removed:
            await self._rewrite_stream(topic, p, survivors)
        return removed

    async def _rewrite_stream(self, topic: str, p: int, survivors: list) -> None:
        """Replace a partition's stream with the surviving entries (offsets preserved)."""
        key = self._k_stream(topic, p)
        await self._r.delete(key)
        if survivors:
            await self._r.set(self._k_start(topic, p), survivors[0][1]["off"])
            for eid, fields in survivors:
                await self._r.xadd(key, fields, entry_id=eid)
        else:
            end = await self._r.get(self._k_end(topic, p)) or "0"
            await self._r.set(self._k_start(topic, p), end)

    # --- consumer-group offset store ------------------------------------ #

    async def commit_offsets(
        self,
        group_id: str,
        offsets: dict[TopicPartition, int],
        *,
        generation: int | None = None,
        member_id: str | None = None,
    ) -> None:
        # Generation fencing is in-process (coordinator); durable storage in Redis.
        self._coordinator.commit(group_id, {}, generation=generation, member_id=member_id)
        mapping = {f"{tp.topic}/{tp.partition}": str(off) for tp, off in offsets.items()}
        if mapping:
            await self._r.hset(self._k_committed(group_id), mapping)
        self._metrics.incr("offset_commits", group=group_id)

    async def fetch_committed(
        self, group_id: str, partitions: tuple[TopicPartition, ...]
    ) -> dict[TopicPartition, int | None]:
        stored = await self._r.hgetall(self._k_committed(group_id))
        out: dict[TopicPartition, int | None] = {}
        for tp in partitions:
            raw = stored.get(f"{tp.topic}/{tp.partition}")
            out[tp] = int(raw) if raw is not None else None
        return out

    async def list_committed(self, group_id: str) -> dict[TopicPartition, int]:
        stored = await self._r.hgetall(self._k_committed(group_id))
        out: dict[TopicPartition, int] = {}
        for field, value in stored.items():
            topic, _, partition = field.rpartition("/")
            out[TopicPartition(topic, int(partition))] = int(value)
        return out

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
