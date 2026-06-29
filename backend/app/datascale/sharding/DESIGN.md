# Sharding + connection-proxy layer — design & roadmap

Owner domain: **Database at scale, facet A — horizontal sharding + connection
proxy** under `backend/app/datascale/sharding/`.

This package is the *next floor up* from the single-cluster `app.db` layer (typed
engine registry, read/write split, pool health, repositories). It assumes a fleet
of such clusters ("shards") and adds the horizontal-scale data plane on top —
**without rewriting `app.db`**; it composes those primitives one shard at a time.

Read first: `kinora.md` §8 (memory/canon/hashing — the per-book read path that
makes `book_id` the natural shard key) and §12 (the unglamorous 30%: queue,
concurrency, caching, observability — the scale concerns this layer serves).

## Design principles

1. **Import- and construction-safe.** Importing any module here opens no sockets
   and needs no network; engines/pools/clients are created on first use (mirrors
   `app.db` and `composition.py`). `ShardCluster.build(...)` works with
   `DASHSCOPE_API_KEY=test` and no infrastructure.
2. **Pure where it can be, abstracted where it can't.** Routing, planning,
   resharding orchestration and the pool *mechanics* are pure or protocol-driven,
   so they are proven deterministically with fakes. The I/O (real per-shard
   engines, sessions, prepared transactions) lives behind `Protocol` seams in
   `backends.py`.
3. **Reproducible routing.** Every placement decision is a pure function of the
   topology + key, using cryptographic digests (not Python's randomised `hash`),
   so the same key routes to the same shard in any process / restart.
4. **Additive & opt-in.** A single-cluster deployment never instantiates this
   layer. **No shared files were modified** — the package is new directories only.
   It reuses the already-present `config.py` pool/replica knobs (added by the
   `app.db` owner) via `EngineConfig`; it adds none of its own.
5. **Everything typed.** `disallow_untyped_defs = true`, mypy clean.

## Module map (all under `app/datascale/sharding/`)

| Module | Responsibility |
|---|---|
| `keys.py` | `ShardKey` value object: stable, deterministic byte encoding of the routing column(s); single/compound keys; modulo + digest helpers. |
| `topology.py` | `Shard` / `ShardTopology`: immutable description of the fleet + per-shard lifecycle state (ACTIVE/READ_ONLY/DRAINING/OFFLINE) driving resharding & routing safety. Pure transitions return new topologies. |
| `strategy.py` | The four placement policies behind `ShardStrategy`: `ModuloHashStrategy`, `RangeStrategy` (contiguous bounds + range-query pruning), `DirectoryStrategy` (pin table + fallback), `ConsistentHashStrategy` (virtual nodes, weighting, replica walk). |
| `slots.py` | Fixed hash-slot model (Redis-Cluster shaped): `SlotMap` + `SlotStrategy` so resharding is slot *reassignment*, not a per-key move. Balanced assignment, minimal-movement add/remove, `migration_set` diff. |
| `router.py` | `ShardRouter`: resolves a query → shard(s) with state-aware write/read safety + a `MigrationOverlay` (dual-write/cutover seam). Single-key fast path, range scatter, keyless scatter. |
| `planner.py` | `QueryPlanner` → `ScatterPlan`: single-shard vs scatter; LIMIT push-down (offset+limit per shard for a correct global top-n); aggregate decomposition (distributive fold, AVG→SUM/COUNT rewrite, holistic flag); GROUP BY. `explain()`. |
| `executor.py` | `ScatterGatherExecutor`: concurrent fan-out + the gather recipes (passthrough / concat / k-way merge-sort with global offset/limit / aggregate fold / group-aggregate). FAIL_FAST vs PARTIAL modes. `FakeShardExecutor` for tests. |
| `transaction.py` | Distributed-tx seam: `TwoPhaseCommitCoordinator` (prepare→commit/abort, in-doubt handling) and `SagaCoordinator` (forward steps + reverse compensations, compensation-failure capture). |
| `resharding.py` | Online split/move state machine `PLANNING→DUAL_WRITE→BACKFILL→VERIFY→CUTOVER→CLEANUP→DONE` (+ `ABORTED` pre-cutover). Batched backfill, checksum verify, atomic overlay cutover, rollback-safe. `InMemoryReshardMover` for tests. |
| `rebalance.py` | Rebalance planner: an add/remove-shard topology change → an ordered, *estimated* `RebalancePlan` (exact slot moves, or sampled ring-move estimates) with inflow/outflow accounting. |
| `proxy.py` | pgbouncer-shaped connection proxy: **transaction pooling**, multiplexing many logical clients over few backends, a **bounded fair FIFO wait queue** with timeout + fail-fast backpressure, pre-ping health checks, recycle-on-age, `ProxyStats`. `ShardProxyPool` = one proxy per shard. |
| `backends.py` | Production adapters: `ShardEngineRegistry` (one `app.db` `EngineRegistry` per shard), `EngineBackendFactory` (proxy backend over a real connection), `SessionShardExecutor` (real SQL scatter), `SessionTwoPCParticipant` (Postgres `PREPARE TRANSACTION`). |
| `cluster.py` | `ShardCluster`: the one facade composing router + planner + executor + resharding; owns the live `MigrationOverlay` so an in-flight reshard auto-publishes into routing. |

## Roadmap (phase by phase — all delivered)

- **Phase 1 — keys + topology.** ✅ Stable key encoding; immutable fleet model + states.
- **Phase 2 — strategy framework.** ✅ Hash / range / directory / consistent-hash (vnodes, weights, replicas).
- **Phase 3 — router.** ✅ State-aware single/range/scatter routing + migration overlay.
- **Phase 4 — planner.** ✅ Scatter plans, limit push-down, aggregate decomposition, group-by.
- **Phase 5 — scatter-gather executor.** ✅ Concurrent fan-out + all gather recipes + failure modes.
- **Phase 6 — distributed transactions.** ✅ 2PC coordinator + saga runner.
- **Phase 7 — online resharding.** ✅ Dual-write → backfill → verify → cutover → cleanup, rollback-safe.
- **Phase 8 — connection proxy.** ✅ Transaction pooling, multiplexing, bounded fair queue, health.
- **Phase 9 — slots + rebalance.** ✅ Fixed-slot model + topology-diff rebalance planner.
- **Phase 10 — production wiring + facade.** ✅ Per-shard engines, session executor, 2PC participant, `ShardCluster`.

## Correctness highlights proven by tests

- **Consistent hashing minimises movement.** Going 3→4 shards remaps < 45% of
  keys (vs > 60% for modulo-hash), and the moved keys land overwhelmingly on the
  new shard — the resharding-friendliness claim, measured.
- **Global top-n across shards.** The k-way merge with `offset+limit` push-down
  returns the true global ordering even when all of the top-n live on one shard
  (the case naive per-shard `LIMIT n/shards` gets wrong).
- **AVG is recombined correctly** from per-shard SUM/COUNT partials (not the mean
  of per-shard means).
- **2PC atomicity:** a single NO vote (or prepare error) aborts everywhere; a
  committed-but-unreachable participant is reported *in-doubt*, never silently
  lost.
- **Saga compensation** runs in reverse over completed steps; a stuck
  compensation is recorded but does not strand the others.
- **Resharding** moves the right rows, verifies by checksum, cuts over atomically
  at the overlay, refuses to abort past cutover, and rolls back the target on a
  pre-cutover verify mismatch.
- **Proxy** caps real backends at `pool_size` under 8 concurrent clients,
  serves waiters FIFO-fairly, times out / fails-fast on a full queue, and
  recycles dead/aged backends.

## Test inventory

All under `tests/datascale/` (≈ 1,500 lines of tests across 12 files):

- `test_sharding_keys.py` — 16 unit tests (encoding stability, boundary safety, golden hash).
- `test_sharding_topology.py` — 8 unit tests (states, validation, pure transitions).
- `test_sharding_strategy.py` — 24 unit tests (all four strategies, movement & balance properties).
- `test_sharding_router.py` — 9 unit tests (state-aware routing, overlay dual-write/cutover).
- `test_sharding_planner.py` — 11 unit tests (scatter modes, limit push-down, agg rewrite, group-by).
- `test_sharding_executor.py` — 12 unit tests (all gather recipes, top-n, AVG, partial/fail-fast).
- `test_sharding_transaction.py` — 10 unit tests (2PC commit/abort/in-doubt, saga compensation).
- `test_sharding_resharding.py` — 12 unit tests (full protocol, batching, verify-abort, rollback).
- `test_sharding_proxy.py` — 13 unit tests (pooling, multiplexing, queue fairness, timeout, health).
- `test_sharding_cluster.py` — 5 unit tests (end-to-end query + reshard auto-publish into router).
- `test_sharding_slots.py` — 14 unit tests (balanced map, slot invariance, minimal-move add/remove).
- `test_sharding_rebalance.py` — 7 unit tests (slot/ring plans, conservation, topology delta).
- `test_sharding_backends.py` — 7 unit tests (import-safety, registry wiring, SQL builder, gid namespacing).
- `test_sharding_integration_db.py` — 4 **Postgres-integration** tests (real SQL scatter top-n,
  COUNT, real-data reshard move+verify+cleanup, partial failure). Multi-shard
  fleet simulated with separate schemas; SKIP when `KINORA_TEST_DATABASE_URL`
  unset, run against an isolated `kinora_sharding_test` DB on :5433 (never the
  live `kinora` DB).

153 unit tests pass with no infra; the 4 integration tests pass against the
isolated DB. `mypy app` clean (full app, 796 files). `ruff` clean for the
package (the only repo-wide `ruff` findings are pre-existing in
`migrations/versions/` — untouched here).

## Additive shared-file changes

**None.** The package is entirely new directories
(`app/datascale/`, `tests/datascale/`). It consumes the existing
`core/config.py` replica/pool knobs and `app.db.engine` / `app.db.routing`
read-only; it modifies no shared file, no other domain's models/repos, and not
the composition root. Wiring it into `composition.py` behind a feature flag is a
deliberate future step for the owner who turns sharding on.

## Remaining roadmap (future, optional)

- Wire a `ShardCluster` into `composition.py` behind a `sharding_enabled` flag
  + a `shards.yaml`/env-driven topology loader (kept out here to avoid touching
  the shared composition root).
- A `kinora-admin shards` CLI surface (status, plan-rebalance, run-reshard,
  proxy-stats) over the existing CLI framework.
- A `/metrics` projection of `ShardProxyPool.stats()` + per-shard pool health for
  the §12.5 observability panel (owned by the API domain).
- A durable reshard-job journal (the in-memory `ReshardProgress` is the resume
  point; persisting it makes a coordinator crash fully recoverable).
- 2PC in-doubt recovery sweep (re-drive `COMMIT/ROLLBACK PREPARED` from
  `pg_prepared_xacts`), the operational counterpart to the in-doubt reporting.
