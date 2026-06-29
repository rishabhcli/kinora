# `app.distributed.replication` — multi-region active-active replication

> Distributed systems, **facet B**: a geo-distributed data layer. Pure protocol
> logic, dependency-free, exhaustively property-tested for convergence,
> commutativity, and partition tolerance. No DB, no network, no clock side
> effects, zero model spend — every collaborator (clock, transport, RNG) is
> injected so a whole cluster is a pure function of its inputs.

## Why this exists / how it relates to the rest of the codebase

Kinora's canon already has a CRDT layer at `app.memory.crdt` (HLC + LWW/OR-set/
G-counter/version-vector) for **per-entity, single-store** concurrent canon
edits. This package is **broader and distinct**: a cluster-wide **replication
protocol across regions** — the layer that would let Kinora run active-active in
multiple Alibaba regions, each serving reads/writes locally and converging
asynchronously. The two never import each other; this one re-derives its own
region-aware primitives (a `(region, node)` HLC, a log-frontier version vector)
because its job is replication, not entity versioning.

This is an **additive, self-contained new package**. The only shared-file touch
is additive (see "Shared-file changes" below).

## Architecture (bottom-up)

| Module | Responsibility |
|---|---|
| `clock.py` | Hybrid-logical-clock timestamping. `NodeId` (`region/node`), `HybridTimestamp` (globally **totally ordered** `(wall, logical, node)`), `HybridLogicalClock` (Kulkarni HLC rules + bounded-skew clamp), `ManualClock` (deterministic time). |
| `version.py` | `VersionVector` = a per-node **log frontier** with the partial order: `dominates`, `concurrent_with`, `merge` (CRDT join), `missing_ranges` (anti-entropy digest). |
| `conflict.py` | **Pluggable conflict resolution.** `LWWResolver`, CRDT values (`GCounterValue`, `PNCounterValue`, `ORSetValue`, `MVRegisterValue` — the last is causality-correct via dotted version vectors), `CustomResolver` (app-defined merge with a determinism guard), and `ResolverRegistry` (longest-prefix key → resolver). Every resolver obeys commutativity/associativity/idempotence. |
| `log.py` | `ReplicationRecord` (origin, per-origin seq, HLC stamp, causal `deps`) + `ReplicationLog` (append-only, gap-free per-origin segments, `delta_since` in causal-safe timestamp order). The async-log-shipping substrate. |
| `store.py` | `ReplicaStore` — one region's materialized keyspace; `apply` is idempotent + order-independent through the bound resolver; `merge_cell` is the Merkle-repair path (value/timestamp merge, no frontier mutation). `KeyAffinity` = per-key region placement. |
| `node.py` | `ReplicaNode` — clock + log + store + **causal-delivery buffer**: a remote record applies only when its deps are met and it is the next per-origin seq, else it is parked and drained to fixpoint when unblocked. |
| `merkle.py` | Fixed-shape Merkle trees over the keyspace; `diff_buckets` finds divergent buckets in `O(d·log n)` so anti-entropy ships only what differs. |
| `antientropy.py` | `Reconciler`: version-vector **delta sync** (common path) + **Merkle repair** (post-partition / log-compaction safety net). |
| `consistency.py` | Tunable consistency: `ConsistencyLevel` ONE/QUORUM/ALL (`R+W>N` overlap rule), `WriteCoordinator`/`ReadCoordinator` ack/answer counting, `StalenessPolicy` (bounded-staleness reads). |
| `routing.py` | Geo-routing: `RegionTopology` (latency matrix), `PlacementPolicy` (affinity → replica regions), `GeoRouter` (local → home → nearest-live replica, deterministic tiebreaks). |
| `failure.py` | Partition detection + healing: `FailureDetector` (heartbeat timeout), `PhiDetector` (accrual), `PartitionMonitor` (emits PARTITIONED/HEALED transitions; HEALED is the cue to reconcile). |
| `gossip.py` | `GossipEngine.tick`: deliver inbound → react to heals (Merkle repair) → push new records → pull from a round-robin peer. The orchestration glue. |
| `transport.py` | `Transport` seam + `InMemoryFabric` (deterministic latency/drop/reorder/`Partition` injection) + `DirectTransport`. |
| `simulator.py` | `MultiRegionSimulator` — runs a declarative `Scenario` (writes + partition/heal timeline + clock skew + lossy network) to quiescence and reports a convergence verdict. `assert_converged` is the reusable oracle. |

## The convergence guarantee

Strong eventual consistency: **after writes stop and the network heals, every
replica holds byte-identical state.** It rests on three proven properties:

1. **Resolvers obey the CRDT laws** (commutativity, associativity, idempotence)
   — property-tested over thousands of randomized value populations.
2. **The store applies records idempotently and order-independently** — tested
   over every legal delivery interleaving.
3. **Anti-entropy closes every gap** the lossy/partitioned network leaves —
   tested end-to-end at 50% packet loss and across partition/heal cycles.

The HLC total order (with its `node` tiebreak) is what makes LWW resolve
*identically* on every replica; the version-vector frontier + causal-delivery
buffer is what preserves causality across regions.

## Determinism

No wall clock, no real I/O, no unseeded randomness anywhere. A
`MultiRegionSimulator` run is a pure function of its `Scenario` (including the
RNG `seed`), so every property-test failure is reproducible from its seed.

## Testing

`backend/tests/distributed/` — 150+ tests:

- per-module unit tests (`test_clock`, `test_version`, `test_conflict`,
  `test_log`, `test_store`, `test_node`, `test_merkle`, `test_transport`,
  `test_antientropy`, `test_consistency`, `test_routing`, `test_failure`,
  `test_gossip`, `test_simulator`);
- `test_properties.py` — seeded-random property sweeps: CRDT law fuzzing,
  random-delivery-order store convergence, and end-to-end simulator fuzzing over
  random write/partition timelines.

Run: `cd backend && .venv/bin/pytest tests/distributed -q`
Lint/type: `.venv/bin/ruff check app/distributed tests/distributed` ·
`.venv/bin/mypy app/distributed`

## Shared-file changes (additive only)

None required for the protocol logic — the package is fully self-contained and
imports only stdlib + sibling modules. No Alembic migration is added (this layer
is pure in-memory protocol; persistence is a future adapter behind the
`ReplicationLog`/`ReplicaStore` seams). If/when wired to the app, the intended
seam is a storage adapter implementing the log append/read surface and a
transport adapter implementing `Transport` over the real message bus.
