# Streaming data plane — facet A: the partitioned log (`backend/app/streaming/log/`)

> **Owner:** Agent (streaming data-plane, facet A). **Status:** living roadmap.
>
> A Kafka/Redpanda-shaped **streaming substrate**: a partitioned, offset-addressed,
> replayable log that arbitrary producers append to and arbitrary consumer groups
> read from at their own pace. This is the general event-streaming plane — it is
> **not** the render queue (`app.queue`), which is a Redis priority queue for one
> job type. This log is the seam the two sibling facets — stream **processing** and
> **CDC** — are built on. They depend on the `Broker` protocol below and nothing
> else in this package.

## Why a log and not a queue

The render queue (`app.queue`) answers "who renders this shot next, and never
twice." It is destructive (a job is claimed and gone) and single-purpose. A
streaming *log* answers a different question: "let any number of independent
consumers replay an ordered, durable history of events at their own offset."
That is what stream processing (windowed aggregations, joins) and CDC (replicate
every row change in order, re-readable after a crash) require. Offsets +
retention + consumer groups are the primitives those facets cannot get from the
queue, so this facet provides them once, cleanly, behind one protocol.

## The `Broker` protocol — the contract sibling facets consume

`app.streaming.log.broker.Broker` is the **only** surface the processing and CDC
facets (and the high-level `Producer`/`Consumer`) are written against. Two
implementations satisfy it interchangeably:

| Implementation | Module | Use |
|---|---|---|
| `InMemoryBroker` | `memory/broker.py` | zero-infra (tests, single-process) |
| `RedisStreamsBroker` | `redis/broker.py` | production / multi-process (Redis Streams) |

The protocol is **log-shaped, not queue-shaped**. Method families:

- **Admin** — `create_topic` / `delete_topic` / `topics` / `describe_topic` /
  `partitions_for`.
- **Produce** — `produce(topic, partition, *, key, value, timestamp_ms, headers,
  ctx)` → `RecordMetadata`. The partition is already resolved by the caller
  (the `Producer` runs the partitioner). `ctx: ProduceContext` carries the
  exactly-once metadata (producer id, epoch, per-partition sequence, transaction
  flags); a plain `ProduceContext()` is a non-idempotent append.
- **Consume** — `fetch(topic, partition, offset, *, max_records, max_bytes)` →
  `FetchResult(records, next_offset, high_watermark, log_start_offset)`. Plus
  `beginning_offsets` / `end_offsets` / `offsets_for_times` for bounding seeks.
- **Group offset store** — `commit_offsets` / `fetch_committed` /
  `list_committed`. A committed offset is the **next** offset to read.
- **Group membership** — `join_group` / `leave_group` / `heartbeat` /
  `describe_group`, returning `JoinResult(member_id, generation, assignment,
  is_leader)`.

### Consuming the protocol (sibling-facet recipe)

```python
from app.streaming.log import Broker, Consumer, ConsumerConfig, Producer, ProducerRecord

class MyProcessor:
    def __init__(self, broker: Broker) -> None:          # depend on the protocol
        self._consumer = Consumer(broker, config=ConsumerConfig(group_id="proc"))
        self._producer = Producer(broker)                # for write-back

    async def run(self, source_topic: str) -> None:
        await self._consumer.subscribe((source_topic,))
        async for record in self._consumer:
            ...                                          # transform
            await self._consumer.commit()
```

For end-to-end exactly-once a facet uses a transactional `Producer` and
`send_offsets_to_transaction` (see *Exactly-once* below).

## Layout

```
streaming/log/
  __init__.py            ← public surface (re-exports everything below)
  DESIGN.md              ← this file
  errors.py              ← StreamingError hierarchy (topic/offset/seq/fence/txn/group)
  record.py              ← ProducerRecord / ConsumerRecord / RecordMetadata / TopicPartition
  partitioner.py         ← murmur2 (Kafka-compatible) + Default/RoundRobin/Sticky
  topic.py               ← TopicConfig + CleanupPolicy + RetentionPolicy
  partition.py           ← PartitionLog: the append log (offsets, segments, retention, compaction)
  broker.py              ← the Broker protocol + ProduceContext / JoinResult / GroupDescription
  producer.py            ← Producer: partitioning, batching, idempotence, transactions
  consumer.py            ← Consumer: assign/subscribe, poll, offsets, seek, pause/resume, lag, streaming
  cleaner.py             ← LogCleaner: background retention/compaction driver (Broker.maintain loop)
  admin.py               ← Admin: idempotent topic create, describe, group-lag introspection
  serde.py               ← Serde seam + String/Json/Bytes codecs + TypedProducer/TypedConsumer
  metrics.py             ← MetricsSink protocol + InMemoryMetrics/NullMetrics (broker emission)
  cli.py                 ← standalone operator CLI (python -m app.streaming.log.cli)
  group/
    assignor.py          ← Range / RoundRobin / CooperativeSticky assignment strategies
    coordinator.py       ← GroupCoordinator: membership, generations, rebalance, offset store
  memory/broker.py       ← InMemoryBroker (full protocol, no infra)
  redis/
    client.py            ← StreamRedis seam + RedisStreamAdapter (real) + FakeStreamRedis (double)
    broker.py            ← RedisStreamsBroker (one Redis Stream per partition)
```

## Operational layer (`cleaner.py`, `admin.py`, `serde.py`, `metrics.py`, `cli.py`)

All five depend only on the `Broker` protocol, so they work over either broker:

- **`LogCleaner`** runs `Broker.maintain` on a fixed interval as an `asyncio` task
  (the Kafka `LogCleaner` analogue) — retention + compaction without a hot-path
  call. Injectable clock/sleep make it deterministic in tests; a bad sweep is
  counted, not fatal.
- **`Admin`** composes existing broker calls into operator queries: idempotent
  `create_topic_if_absent`/`ensure_topics`, a `describe` (config + per-partition
  offset windows), and `group_lag` (`end_offset − committed`, full retained count
  where the group never committed).
- **`Serde` + `TypedProducer`/`TypedConsumer`** let the sibling facets work in
  domain types (`str`, JSON, `bytes`, or a custom codec / schema registry) while
  the log stays `bytes`-native. Tombstones round-trip (`serialize(None) is None`).
- **`MetricsSink`** is a two-method protocol (`incr`/`observe`) the brokers emit
  to — `records_produced`/`records_fetched`/`fetch_requests`/`offset_commits`/
  `rebalances`/`records_cleaned`/`records_deduplicated`, labelled by topic/group.
  Default `NullMetrics` costs nothing; `InMemoryMetrics` snapshots for a
  `/metrics` endpoint or tests.
- **`cli.py`** is a standalone operator CLI (`python -m app.streaming.log.cli …`):
  `topics` / `describe` / `lag` / `tail`. It builds a `RedisStreamsBroker` from a
  URL and is intentionally *not* wired into the `kinora-admin` tree, keeping this
  facet self-contained.

## The append log (`partition.py`)

The core data structure. Per partition:

- **Offsets.** `log_start_offset` (oldest retained) and `log_end_offset` (next to
  assign) bound the readable window. Offsets are dense and never reused, so a
  committed offset keeps a stable meaning even after retention/compaction.
- **Segments.** Records live in roll-bounded segments (`segment_bytes`). Retention
  deletes **whole segments** only — so `log_start_offset` jumps in segment steps,
  exactly like Kafka, and the active segment is never evicted.
- **Retention (`CleanupPolicy.DELETE`).** Age (`retention_ms`) and/or size
  (`retention_bytes`) bounded; `-1` disables a dimension.
- **Compaction (`CleanupPolicy.COMPACT`).** Keep only the latest record per key.
  Records younger than `min_compaction_lag_ms` form an un-compacted tail.
  Tombstones (`value is None` on a keyed record) survive `delete_retention_ms` so
  consumers observe the delete, then are reaped. Keyless records are never
  deduplicated.

`PartitionLog` is single-writer; the broker serialises per-partition access (an
`asyncio.Lock` in memory; Redis natively). The Redis broker re-implements the same
semantics over a Redis Stream (`xadd`/`xrange`/`xtrim minid`, with compaction by
rewriting the stream) so both brokers pass one shared contract test suite.

## Producers, idempotence, transactions (`producer.py`)

- **Partitioning** via `DefaultPartitioner` (keyed → `murmur2`, keyless → sticky),
  Kafka-wire-compatible so placement agrees with real Kafka clients.
- **Batching** — `send` buffers per-partition and returns a future resolving to
  `RecordMetadata`; `flush` (or `batch_size`/`linger_ms`) drains.
- **Idempotence** — a stable `producer_id` + per-partition monotonic sequence; the
  broker de-duplicates a replayed send and raises `SequenceError` on a gap.
- **Transactions** — `transactional_id` upgrades the producer to exactly-once:
  `begin/commit/abort_transaction` make a set of appends atomic, and
  `send_offsets_to_transaction` folds the consumer offsets that produced them into
  the same atomic unit. `async with producer.transaction():` commits on success,
  aborts on exception. Producer **fencing** (`FencedProducerError`) rejects a
  zombie producer whose epoch is stale.

## Consumer groups + rebalancing (`consumer.py`, `group/`)

- **Assignment** — explicit (`assign`) or group-managed (`subscribe` + `group_id`).
  On a membership/subscription change the `GroupCoordinator` bumps a monotonic
  **generation** and recomputes the assignment via the configured `Assignor`.
- **Strategies** — `range` (contiguous per-topic), `roundrobin` (balanced across
  topics), `cooperative-sticky` (minimise churn: keep prior ownership, reassign
  only the surplus — enables incremental rebalance).
- **Fencing** — a `heartbeat`/`commit` at a stale generation reports rejoin-needed
  (`False`) / raises `CommitConflictError`; a dead member is evicted past the
  session timeout (deterministic via an injectable clock).
- **Offsets** — manual `commit` or `enable_auto_commit`; committed offset == next
  to read. `lag()` / `end_offsets()` expose how far behind the head each
  partition is — the substrate's core health signal.

## Exactly-once (read-process-write)

```python
producer = Producer(broker, config=ProducerConfig(transactional_id="etl-1"))
await producer.init_transactions()
batch = await consumer.poll()
async with producer.transaction():
    for r in batch:
        await producer.send(ProducerRecord.from_json("out", transform(r.json()), key=r.key_str()))
    await producer.send_offsets_to_transaction(
        {tp: consumer.position(tp) for tp in consumer.assignment}, consumer._config.group_id
    )
# Output records AND the input offsets commit atomically — or neither does.
```

## Testing strategy

- **Zero-infra by default.** Every behaviour is unit-tested against `InMemoryBroker`
  **and** `RedisStreamsBroker` over `FakeStreamRedis` (one parametrised fixture), so
  the protocol holds identically on both.
- **Live wiring** — `test_streaming_redis_live.py` runs the real `RedisStreamAdapter`
  against a Redis (gated on `KINORA_TEST_REDIS_URL`; use an isolated db, e.g. db 15),
  confirming the one thing the fake cannot: the adapter speaks real Redis. (This
  test already caught the `XTRIM ... MINID approximate=True` no-op default.)
- Tests live in `backend/tests/test_streaming_*.py`.

## Roadmap (facet A)

Built:
- [x] Records, Kafka-compatible partitioners, topic/retention/compaction config.
- [x] The append log: offsets, segment rolling, time/size retention, log compaction + tombstones.
- [x] `Broker` protocol (incl. `maintain`); in-memory + Redis-Streams implementations (shared contract suite).
- [x] Producer: partitioning, batching, idempotence, transactions (EOS).
- [x] Consumer: assign/subscribe (multi-topic), poll, offset reset/commit, seek, **pause/resume**, lag, async streaming.
- [x] Consumer groups: range/round-robin/cooperative-sticky assignment, generations, rebalance, fencing, heartbeats.
- [x] Exactly-once read-process-write (offsets folded into the producer transaction).
- [x] **Background cleaner** (`LogCleaner`) — periodic `maintain()` asyncio driver.
- [x] **Admin client** (`Admin`) — idempotent topic create, describe, group-lag introspection.
- [x] **Serde seam** (`Serde` + `TypedProducer`/`TypedConsumer`) — String/Json/Bytes codecs, custom codecs welcome.
- [x] **Metrics** (`MetricsSink` + `InMemoryMetrics`) emitted by both brokers (produce/fetch/commit/rebalance/clean/dedup).
- [x] **Operator CLI** (`python -m app.streaming.log.cli`): topics / describe / lag / tail.

Next (additive, no breaking changes to the protocol):
- [ ] **Multi-broker fan-out** — a `topics` namespace prefix so several logical
  logs share one Redis without key collisions (namespace is already a ctor arg).
- [ ] **Prometheus adapter** — a `MetricsSink` over `prometheus_client` /
  `app.observability` (the seam already exists; only the adapter is left).
- [ ] **Composition wiring** — expose a `Broker` from `app.composition.Container`
  (a `streaming_broker` DI seam) once a sibling facet needs it in-process; until
  then the package stays self-contained and importable on its own.
- [ ] **Lifespan integration** — start a `LogCleaner` per broker in the `api`
  process lifespan once the broker is wired into the container.

## Shared-file changes

This facet is **additive-only**. It introduces the new package
`backend/app/streaming/` and new `backend/tests/test_streaming_*.py`; it does not
modify any pre-existing module. No composition-root, config, or model changes are
required to import and use it.
