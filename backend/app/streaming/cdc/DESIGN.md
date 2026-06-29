# `app.streaming.cdc` — change-data-capture + incremental materialised views (DESIGN / roadmap)

> **Facet C of the streaming data plane.** A self-contained subsystem that
> captures every change to the authoritative Postgres store, normalises it into
> typed change events, and maintains denormalised read models *incrementally* so
> the product reads (library shelf, canon-graph projection, render-progress
> counters) are O(1) lookups that stay consistent with the source.

Reads: kinora.md **§8** (the canon memory layer — the read models this projects),
**§12.1–§12.5** (queue, backpressure, caching layers, observability — the
engineering posture this mirrors).

This package is **additive and self-contained**. The only edits outside it are
two additive table registrations (see *Additive shared-file changes* below); it
adds **no** runtime dependency to any existing module and is import-safe with
`DASHSCOPE_API_KEY=test` and no network/DB.

## Why this exists

The canon (§8) and the product's read surfaces are *derived* from the
operational tables. Today every read re-queries and re-joins. As books, shots,
and canon versions grow, those reads get more expensive and the "what does the
shelf / canon graph look like *right now*" question becomes a scan. The CDC
plane turns the derivation into a **stream**: changes flow out of Postgres once,
and each derived view is kept current by applying a tiny delta per change. This
is the same reframe §8 makes for consistency ("a memory problem, not a model
problem") applied to reads ("a streaming problem, not a query problem").

It also gives the system a clean **event backbone**: the same typed change
stream that feeds the view engine can feed a broker topic (sibling facet A),
cache-invalidation, search-index updates, or analytics — without each consumer
re-implementing capture.

## Architecture

```
        Postgres (authoritative)
                │
   ┌────────────┴─────────────┐
   │  CDCSource (one of)       │
   │  • PostgresLogicalSource  │  WAL / logical replication (wal2json decode)
   │  • PollingSource          │  updated_at high-water + tombstone fallback
   │  • FakeChangeStream       │  deterministic test stream
   └────────────┬─────────────┘
                │  ChangeEvent (typed, totally-ordered by LogPosition)
   ┌────────────▼─────────────┐
   │  SnapshotCoordinator      │  consistent snapshot → stream cutover
   └────────────┬─────────────┘
   ┌────────────▼─────────────┐
   │  CDCPipeline              │  schema-migrate · dedup · checkpoint (OffsetStore)
   └────────────┬─────────────┘
                │
   ┌────────────▼─────────────┐
   │  MeteredSink → FanoutSink │  metrics + tee
   └───────┬───────────┬───────┘
           │           │
  MaterializedViewEngine   BrokerSink / RedisStreamSink  (→ facet A / pub-sub)
   • LibraryShelfView          (duck-typed; no import coupling)
   • CanonGraphView
   • AggregateView(s)
   • EquiJoinView(s)
```

| Module | Responsibility |
|---|---|
| `clock.py` | `Clock` protocol, `SystemClock`, **`FakeClock`** (deterministic lag/cadence). |
| `events.py` | `ChangeEvent` (insert/update/delete/read/schema/heartbeat), `LogPosition` (totally ordered), `Op`. The wire contract. |
| `source.py` | `CDCSource` ABC + **`FakeChangeStream`** (scripted deterministic stream) + `ReplayBuffer`. |
| `wal_source.py` | `PostgresLogicalSource` + the pure `decode_wal2json` decoder + `parse_lsn`; `WalReader` port (fake/real). |
| `polling_source.py` | `PollingSource` — `updated_at` high-water + **tombstone strategy** for deletes; `RowFetcher` port + `ListRowFetcher` fake. |
| `snapshot.py` | `SnapshotCoordinator` — the consistent snapshot→stream cutover state machine. |
| `schema.py` | `SchemaRegistry` + `TableSchema` + `reconcile`/`migrate_row` — schema-evolution (add/drop/rename/retype, breaking-pk detection). |
| `offsets.py` | `OffsetStore` protocol, `InMemoryOffsetStore`, `DbOffsetStore` (resumable checkpoints). |
| `sink.py` | `ChangeSink` protocol, `InMemorySink`, `FanoutSink`, `BrokerSink` (duck-typed facet A), `RedisStreamSink`, `NullSink`. |
| `pipeline.py` | `CDCPipeline` — source→migrate→dedup→sink with checkpointing; at-least-once. |
| `metrics.py` | `CdcMetrics` (throughput, lag, dedup, view-freshness) + `MeteredSink` decorator (§12.5). |
| `db_adapters.py` | `SqlAlchemyRowFetcher` (polling over real ORM models, **compound keyset cursor**) + `ViewStateCheckpointStore` (persist/rehydrate a view). |
| `runner.py` | `CDCRunner` + `build_kinora_views` — the composition seam + canonical Kinora projection set. |
| `worker.py` | The long-running connector process (`python -m app.streaming.cdc.worker`): `run_cdc_cycle`, `run_worker_loop`, `PipelineFactory`. Degrades to a no-op with no DB. |
| `service.py` | `ViewReadService` — framework-agnostic read façade over the engine (with checkpoint fallback). No HTTP route registered (additive seam only). |
| `models.py` | ORM: `cdc_offsets`, `cdc_view_state` (additive; migration `cdc_0001`). |
| `views/` | The incremental view engine (see below). |

### The view engine (`views/`)

| Module | Responsibility |
|---|---|
| `delta.py` | The **Z-set** (weighted multiset) algebra: `Row`, `ZSet`, `Delta`, insert/update/delete deltas. Makes insert/update/delete uniform and maintenance associative. |
| `graph.py` | `DependencyGraph` — table/view DAG; topological order + transitive dirty-set + cycle detection. |
| `view.py` | `MaterializedView` ABC (incremental `on_event` + from-scratch `recompute` oracle) and `KeyedProjectionView` (1:1 projection base). |
| `engine.py` | `MaterializedViewEngine` — registration, routing, ordered maintenance, the **consistency oracle** (`verify`). Is itself a `ChangeSink`. |
| `aggregate.py` | `AggregateView` (GROUP BY) with **invertible** reducers: count/sum/avg/min/max/distinct — correct under deletes and group-key changes. |
| `join.py` | `EquiJoinView` — symmetric-hash incremental inner equi-join with two-sided delta propagation. |
| `library_shelf.py` | Concrete: one denormalised shelf card per (non-deleted) book. |
| `canon_graph.py` | Concrete: present-version entity nodes + active continuity-state edges (§8.1/§8.5 versioning). |

## Key design decisions

* **Total order via `LogPosition`.** Every event carries a comparable
  `(major, minor)` position (WAL LSN+sub-index, or polling `updated_at`+tiebreak).
  Ordering, resume, dedup, and the snapshot consistency point are all `<=`
  comparisons — no locks.
* **Z-sets unify the three mutations.** An update is `{old: -1, new: +1}`; a
  delete is `{row: -1}`. The view never special-cases the op, and maintenance is
  provably the same as a from-scratch recompute — which `engine.verify` asserts
  in tests (the IVM correctness oracle).
* **Snapshot → stream cutover is a position comparison.** The coordinator
  resumes the stream strictly after the snapshot's consistency point
  (`snapshot_low_water`, or `LogPosition.zero()` when the source can't expose
  it — always correct under at-least-once, the pipeline dedups). No event lost,
  none double-counted.
* **Deletes need a tombstone strategy under polling.** An `updated_at` query
  can't see a vanished row, so the polling fallback relies on soft-delete
  (`deleted_at`) and emits a `DELETE` for newly-tombstoned rows. WAL sees deletes
  natively.
* **No import coupling to facet A.** The pipeline depends only on the
  `ChangeSink` protocol; `BrokerSink` adapts any object with a `publish(topic,
  payload)` coroutine, so a sibling `Broker` drops in the moment it lands.
* **Infra is optional.** Sources, the engine, sinks, offsets, and metrics are all
  exercised deterministically with fakes (`FakeChangeStream`, `ListRowFetcher`,
  `ListWalReader`, `InMemoryOffsetStore`, `FakeClock`). DB-backed adapters
  (`DbOffsetStore`, `SqlAlchemyRowFetcher`, `ViewStateCheckpointStore`) are
  isolated in `db_adapters.py`/`offsets.py` and their tests skip without
  `KINORA_TEST_DATABASE_URL`.

## Consistency guarantees

* **At-least-once delivery** to sinks; **idempotent application** in the view
  engine (re-applying the current retract/assert for a key is a no-op via the
  per-key projected-row bookkeeping).
* **Monotonic offsets** — a late/slow commit can never rewind the checkpoint.
* **IVM correctness** — for every view, `engine.verify(base)` recomputes from the
  live source rows and asserts the incrementally-maintained Z-set is identical
  (and holds no negative weights). This is asserted after churn (inserts +
  updates + deletes + key changes) in the test suite.

## Test coverage

`tests/test_cdc_*.py` — all infra-free except the explicitly DB-gated cases:

* `test_cdc_events` — event contract + `LogPosition` order + `FakeClock`.
* `test_cdc_delta_algebra` — Z-set add/retract/cancel/filter, update composition.
* `test_cdc_views_engine` — dependency graph, shelf + canon-graph projections,
  view-of-view routing, consistency oracle.
* `test_cdc_views_aggregate` — count/sum/avg/min-max-under-delete/distinct/where,
  group moves, oracle.
* `test_cdc_views_join` — two-sided propagation, late arrival, update fan-out,
  key-change move, oracle.
* `test_cdc_sources` — fake stream order/resume/snapshot, WAL decode + LSN parse,
  polling insert→update + tombstone delete + snapshot seen-keys.
* `test_cdc_schema_evolution` — fingerprint, add/drop/rename/retype, breaking pk,
  multi-step migration.
* `test_cdc_pipeline` — snapshot bootstrap (no loss/no dup), commit+resume,
  dedup, in-flight schema migration, all sinks (broker/redis/fanout/null), child
  failure isolation.
* `test_cdc_metrics_runner` — throughput/lag/view-lag, metered sink, the
  canonical Kinora projection set end-to-end.
* `test_cdc_offsets` / `test_cdc_db_adapters` — in-memory (unit) + DB-gated
  roundtrips.

## Additive shared-file changes

* `app/db/models/__init__.py` — added `from app.streaming.cdc.models import
  CdcOffset, CdcViewStateRow` (the established additive table-registration hook)
  and the two names in `__all__`. Registers the two new tables on
  `Base.metadata`; touches no existing model.
* `migrations/versions/cdc_0001_streaming_cdc_offsets_view_state.py` — a new
  Alembic head (revision id **`cdc_0001`**, branches off the bitemporal base
  `a1b2c3d4e5f6` exactly like the sibling analytics/finops/media facets) creating
  `cdc_offsets` + `cdc_view_state`. Standalone tables, no FKs into the
  operational schema.

## Roadmap (next phases, not yet built)

1. **A `psycopg` replication-cursor `WalReader`** + slot lifecycle (create slot
   with `pgoutput`/`wal2json`, `confirmed_flush_lsn` → `snapshot_low_water`).
   Decoding + ordering already done and tested; only the transport remains.
2. **Multi-table transactional consistency** — group events by txid so a view
   that joins across tables only observes committed transactions atomically
   (events already carry txid in `meta`).
3. **Wire `ViewReadService` to an HTTP route** (e.g. `GET /views/library_shelf`).
   Deferred only because it touches the shared API router; the service itself is
   built and tested.
4. **Compaction-aware broker topics** — emit `tombstone()` keyed-null records so
   a log-compacted broker topic (facet A) retains only the latest per key.
5. **Window/temporal views** — time-bucketed aggregates (reading-velocity per
   minute) using the existing reducer machinery over a windowing key.

**Built since the first draft:** the CDC worker entrypoint (`worker.py`), the
read service (`service.py`), incremental aggregate + equi-join views, the
metrics plane (`metrics.py`), and the SQLAlchemy adapters (`db_adapters.py`) —
all moved from this roadmap into the shipped surface above.
