# Event Sourcing — the event store (facet A) — DESIGN

Self-contained, production-grade **append-only event store** under
`backend/app/eventsourcing/store/`. It is the foundation tier for the
event-sourcing track: the **domain** facet (aggregates / command handling) and
the **projection** facet (read models / catch-up subscriptions) — the two
sibling agents in this track — consume it through the protocols documented here.
Nothing in this package depends on those facets; the dependency arrow always
points *into* the store.

Kinora's existing canon already has bitemporal versioning (`app.db.models.bitemporal`,
§8.5) and an episodic log (§8.2). This store is the **generic substrate** under
all of that: an ordered, gap-free, optimistic-concurrency event log with a
reliable publish path (transactional OUTBOX + idempotent INBOX). It is additive
— it introduces its own `es_`-prefixed tables and touches no existing one.

## Why an event store (mapping to kinora.md)

- **§6** separates a control plane from a creative/data plane over a shared
  blackboard. An append-only event log is the canonical audit substrate for that
  blackboard: every canon mutation, every scheduler decision, every render
  outcome can be modelled as an immutable fact with global + per-stream order.
- **§8.5 "forgetting is scoping, not deletion"** is exactly event-sourcing
  semantics: facts are never mutated in place; new facts supersede old ones, and
  state at any beat is a fold over the prefix of the log up to that point.
- **§12.1 idempotency + DLQ + reliable publish** is the OUTBOX/INBOX here:
  appends and their published side-effects share one transaction, and a consumer
  records what it has processed so a redelivery is a no-op.

## Layers (bottom-up)

| Module | Role | Purity |
|---|---|---|
| `errors.py` | Exception hierarchy (`EventStoreError`, `OptimisticConcurrencyError`, `StreamNotFoundError`, `DuplicateEventError`, `AppendError`, `SerializationError`) | pure |
| `versioning.py` | `ExpectedVersion` algebra (`ANY`, `NO_STREAM`, `STREAM_EXISTS`, exact int) + the pure optimistic-concurrency check | pure |
| `contracts.py` | Immutable value objects (`EventData`, `RecordedEvent`, `EventMetadata`, `StreamSlice`, `Snapshot`, `OutboxRecord`, `OutboxStatus`, `Checkpoint`, `CheckpointStatus`) **+ the seam protocols** (`EventStore`, `SnapshotStore`, `OutboxRepository`, `InboxRepository`, `CheckpointStore`, `EventSerializer`, `MessagePublisher`) | pure |
| `serialization.py` | `JsonEventSerializer` + `EventTypeRegistry` (logical event-type ⇄ payload schema), metadata envelope, correlation/causation propagation | pure |
| `memory.py` | `InMemoryEventStore` — a complete, spec-correct store for unit tests (zero infra): global order, per-stream order, OCC, snapshots, in-process OUTBOX + INBOX | pure |
| `models.py` | 6 ORM tables (`es_events`, `es_snapshots`, `es_outbox`, `es_inbox`, `es_sequence`, `es_checkpoints`) on `Base.metadata` | DB |
| `sequence.py` | Gap-free global sequence allocation (a dedicated counter row taken `FOR UPDATE` inside the append txn) | DB |
| `postgres.py` | `PostgresEventStore` — append-with-OCC, global + per-stream reads, snapshotting, OUTBOX enqueue, all inside the caller's transaction | DB |
| `snapshot.py` | `SnapshotStrategy` (every-N-events) + `PostgresSnapshotStore` | mixed |
| `outbox.py` | `OutboxRelay` — claims unpublished outbox rows, hands them to a `MessagePublisher`, marks them published; backoff + DLQ on repeated failure | mixed |
| `inbox.py` | `PostgresInboxRepository` — `already_processed` / `mark_processed` (the §12.1 idempotent INBOX) | DB |
| `checkpoint.py` | `InMemoryCheckpointStore` + `PostgresCheckpointStore` — durable projection positions (`es_checkpoints`) | mixed |
| `subscription.py` | `CatchUpSubscription` — drives a handler over the global log from a durable checkpoint; gap-free resume, per-event advance, fail-stop, pause/resume/reset (the projection facet's read primitive) | pure |
| `aggregate.py` | `Aggregate[S]` (pure fold definition) + `AggregateRepository` — snapshot-accelerated rehydration (`load`) + OCC append + cadence snapshots (the domain facet's building block) | pure |
| `publishing.py` | `CollectingPublisher` (test), `RoutingPublisher` (per-topic fan-out), `RedisMessagePublisher` (production transport over the injected `RedisClient`) + `channel_for` policy | mixed |
| `service.py` | `EventStoreFactory` DI seam + `store`/`outbox_repository`/`inbox`/`checkpoints`/`relay`/`subscription`/`aggregate_repository` builders (mirrors the moderation/notifications factory pattern) | mixed |

### Key decisions

- **Two orderings, both total.** Every appended event gets a **global position**
  (monotone, gap-free, store-wide) *and* a **stream version** (0-based, dense,
  per stream). Projections page by global position; aggregates rehydrate by
  stream version. Gap-freeness of the global position is what lets a catch-up
  projection trust "I've seen everything ≤ P" without a tracking-gap window.

- **Optimistic concurrency via expected-version.** `append(stream, events,
  expected_version)` fails with `OptimisticConcurrencyError` if the stream's
  current version ≠ the expectation. `ExpectedVersion` supports `ANY` (no check),
  `NO_STREAM` (must not exist), `STREAM_EXISTS` (must exist), and an exact int
  (last seen version). A unique constraint `(stream_id, version)` is the hard
  backstop even if two writers race past the read-check.

- **Gap-free global sequence.** Postgres `SEQUENCE`/`SERIAL` is *not* gap-free
  (a rolled-back txn burns numbers). We allocate from a single counter row
  (`es_sequence`) taken `SELECT ... FOR UPDATE` inside the same transaction as the
  append, so a rollback returns the numbers and the global order has no holes.
  This serialises appends store-wide; an accepted trade for a clean global log
  (the volume here — canon/scheduler/render facts — is modest). Isolated in
  `sequence.py` so a future high-throughput variant (hash-partitioned counters)
  is a drop-in.

- **Transactional OUTBOX.** `append` optionally writes one `es_outbox` row per
  event *in the same transaction*. Either both the event and its intent-to-publish
  commit, or neither does — no lost or phantom publishes. A separate `OutboxRelay`
  claims unpublished rows `FOR UPDATE SKIP LOCKED`, publishes, and marks them done;
  failures back off and eventually dead-letter.

- **Idempotent INBOX.** A consumer records `(consumer, message_id)` before acting;
  `already_processed` short-circuits a redelivery. Combined with the OUTBOX this
  gives effectively-once processing over an at-least-once transport.

- **Snapshots.** `Snapshot(stream_id, version, state, ...)`; `read_stream` of an
  aggregate loads the latest snapshot ≤ a version and the events *after* it, so
  rehydration is O(events-since-snapshot). The `SnapshotStrategy` decides when to
  take one.

- **The store is transaction-agnostic.** `PostgresEventStore` takes an
  `AsyncSession` and only ever `flush`es — the caller's unit of work owns the
  commit (matches every other Kinora repository). This is what lets the *domain*
  facet append events and write its own read-model rows in one atomic step.

- **In-memory parity.** `InMemoryEventStore` implements the identical protocol
  and identical OCC / ordering / snapshot semantics, so the domain and projection
  facets can unit-test against it with zero infra. A shared conformance suite
  (`tests/test_eventstore_conformance.py`) runs against the in-memory store
  unconditionally and against Postgres when `ES_STORE_TEST` /
  `KINORA_TEST_DATABASE_URL` is set.

## The seam (what the siblings import)

```python
from app.eventsourcing.store import (
    EventStore, EventData, RecordedEvent, EventMetadata, StreamSlice,
    ExpectedVersion, Snapshot, SnapshotStore, SnapshotStrategy,
    OutboxRepository, InboxRepository, OutboxRecord, MessagePublisher,
    CheckpointStore, Checkpoint, CheckpointStatus,
    CatchUpSubscription, SubscriptionResult, EventHandler,
    Aggregate, AggregateRepository, LoadedAggregate,
    RedisMessagePublisher, RoutingPublisher, CollectingPublisher, channel_for,
    EventSerializer, JsonEventSerializer, EventTypeRegistry,
    EventStoreError, OptimisticConcurrencyError, StreamNotFoundError,
    InMemoryEventStore, InMemoryCheckpointStore,
    PostgresEventStore, PostgresCheckpointStore, OutboxRelay,
)
```

- **Domain facet** uses `EventStore.append/read_stream` for command handling and
  `SnapshotStore` for fast rehydration; it handles `OptimisticConcurrencyError`.
  For the common case it can build an `Aggregate[S]` (a pure `(initial, apply,
  serialize, deserialize)` fold) and drive it through `AggregateRepository`, which
  bundles snapshot-accelerated `load` + OCC `append` + cadence snapshots.
- **Projection facet** uses `CatchUpSubscription` (over `EventStore.read_all` +
  a `CheckpointStore`) for durable, gap-free catch-up subscriptions, and
  `InboxRepository` for idempotent per-message consumption. The publish side is a
  `RedisMessagePublisher` driven by the `OutboxRelay`; a subscriber decodes the
  envelope and dedupes via the inbox (at-least-once + idempotent = effectively-once).

## DB tables (all `es_`-prefixed, additive)

- `es_events` — the log. PK `global_position` (BIGINT, gap-free). Unique
  `(stream_id, version)` and unique `event_id`. JSONB `payload` + `event_metadata`
  (carries `correlation_id`/`causation_id`/`actor`/headers).
- `es_snapshots` — `(stream_id, snapshot_type)` PK, `state` JSONB, `version`.
- `es_outbox` — `id`, `event_id` (unique), `global_position`, `topic`, `payload`
  JSONB, `status` (`pending|published|dead`), `attempts`, `available_at`,
  `published_at`, `last_error`. Index on `(status, available_at)`.
- `es_inbox` — PK `(consumer, message_id)`, `processed_at`, `result` JSONB.
- `es_sequence` — the single gap-free counter row (`name` PK, `value` BIGINT).
- `es_checkpoints` — PK `subscription`, `position` (BIGINT), `status`
  (`active|paused|failed`), `events_processed`, `last_error`, `updated_at`.

## Migration

`migrations/versions/eventstore_0001_event_store_core.py`, revision id
`eventstore_0001`, chains the shared trunk head `a1b2c3d4e5f6` (every sibling
subsystem branches off it; the squash-merge linearises the fan-out). Creates all
six tables; purely additive + reversible. Verified: `alembic upgrade` →
autogenerate shows no `es_` drift → `alembic downgrade` drops them cleanly.

## Additive shared-file changes

- `app/db/models/__init__.py` — import the six ORM models (table registration).
- `app/core/config.py` — three optional settings: `es_snapshot_every`,
  `es_outbox_batch`, `es_outbox_max_attempts` (all defaulted; no behavior change).
- `app/composition.py` — an `event_store_factory` seam on `Container` +
  `event_store()` / `build_event_store(session)` (constructed lazily; no eager
  wiring), mirroring `moderation_factory`.

## Test plan (all green; Postgres arm skips cleanly without `ES_STORE_TEST`)

- `test_eventstore_contracts.py` — value-object invariants, `ExpectedVersion`
  algebra, serializer round-trip, registry, metadata/correlation propagation.
- `test_eventstore_conformance.py` — the shared behavioural suite (append/OCC/
  global+stream order/snapshots/outbox) parametrised over in-memory
  (+ Postgres when infra is present).
- `test_eventstore_memory.py` — in-memory-specific edge cases.
- `test_eventstore_outbox.py` — relay claim/publish/mark, backoff, DLQ, inbox idempotency.
- `test_eventstore_subscription.py` — catch-up subscription ordering, durable
  resume, paging, fail-stop, pause/resume/reset, independent subscriptions.
- `test_eventstore_aggregate.py` — aggregate load/fold, OCC append, snapshot
  acceleration (replay only the tail), cadence policy.
- `test_eventstore_publishing.py` — Redis/routing/collecting publishers + relay
  end-to-end over a fake Redis (zero infra).
- `test_eventstore_postgres.py` — gap-free sequence under rollback, OCC race via
  the unique constraint, `FOR UPDATE SKIP LOCKED` relay claiming, inbox conflict,
  durable Postgres checkpoint resume (skipped without `ES_STORE_TEST`).
