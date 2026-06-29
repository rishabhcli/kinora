# Event-Sourcing read-model projections — `app/eventsourcing/projections/`

Owner domain: **the CQRS read side (facet C)** of Kinora's event-sourcing core.

This package folds an append-only **event log** (owned by sibling **facet A**,
the `EventStore`) into queryable **read models**. It is the *read* half of
Command/Query Responsibility Segregation: commands append events; projections
materialise views; reads serve those views. Nothing here appends to the log.

Read first: `kinora.md` **§8** (memory layer — canon / episodic / hashing /
caching), **§8.5** (forgetting = scoping a fact to the interval where it was
true — the basis for as-of/temporal reads), and **§7.2 / §9.7** (continuity
conflict resolution and the per-shot render state machine, the domains the
example projections fold).

---

## The consumed `EventStore` contract (facet A boundary)

This facet **consumes** facet A's store; it does not define the log. The exact
slice it depends on is declared in [`contracts.py`](contracts.py) as a structural
`Protocol`, so:

* the read side compiles and its **entire** test-suite runs against an in-memory
  fake (`InMemoryEventStore`) **before** facet A's concrete store lands, and
* when facet A ships, its store satisfies `EventStore` structurally with **no
  import edge** from facet A into this package (the dependency points one way).

`StoredEvent` — the immutable envelope:

| field | meaning |
|---|---|
| `event_id` | opaque idempotency key; equality/hash is by this alone |
| `stream_id` | the aggregate/stream the event belongs to (e.g. `session:s1`) |
| `stream_version` | 0-based per-stream ordinal |
| `global_position` | globally monotonic ordering (1-based; `0` = "before anything") — **the value projections checkpoint against** |
| `type` | event type string (the handler dispatch key) |
| `payload` | JSON-able dict |
| `recorded_at` | transaction time (UTC), for as-of-time reads |

`EventStore` — **read-only** from this side:

* `read_all(after_position, limit, types)` → catch-up paging over the global stream.
* `read_stream(stream_id, after_version, as_of)` → one aggregate; `as_of` is transaction-time.
* `head_position()` → max global position (for lag math).
* `subscribe(after_position, poll_interval_s)` → an async live tail.

> **If facet A's method names differ**, adapt them in one thin shim that
> satisfies this `Protocol` — do not widen the protocol or reach into facet A's
> internals. The protocol is the contract; keep it minimal.

---

## Delivery semantics: at-least-once + idempotent

The runtime ([`runtime.py`](runtime.py)) processes the global stream in
`global_position` order and checkpoints progress
([`checkpoints.py`](checkpoints.py)). Because the read-model write and the
checkpoint advance are not one distributed transaction, delivery is
**at-least-once**: a crash between applying an event and advancing the checkpoint
replays that event. Two mechanisms keep replay safe:

1. **Position monotonicity** — `advance` only moves a checkpoint *forward*; a
   stale advance is a no-op. The stored value is the highest fully-applied position.
2. **Applied-event dedupe** — before invoking a handler the runtime calls
   `mark_applied(projection, event_id)`; an already-applied event is **skipped**.
   This is what lets even a relative handler ("increment count") survive
   redelivery — the second delivery never reaches the handler.

Handlers should still prefer **absolute upsert** over relative mutation where the
event carries enough state; the dedupe is the backstop for the rest.

**Type-filtered catch-up.** A projection declares the event types it folds
(`interested_in()`, derived from its `@handles` decorators). The runtime asks the
store for a *filtered* stream and, when that stream drains, advances the
checkpoint to the **global head observed before the empty read** — so a filtered
projection reports lag 0 when caught up (not "all the events it skipped") and the
next poll doesn't re-scan the irrelevant tail.

**Errors.** A handler exception is retried `max_retries` times with a fixed
backoff. On exhaustion the runtime records the error (status → `FAULTED`) and
either **stops** (`stop_on_error=True`, the safe default — a poison event must
not be silently dropped) or **dead-letters + skips** (`stop_on_error=False`) so a
non-critical projection keeps making progress.

---

## Stores (pluggable, protocol-backed)

| Store | Protocol | In-memory (tests/embedded) | Postgres (prod) |
|---|---|---|---|
| Read models | `ReadModelStore` | `InMemoryReadModelStore` | `PostgresReadModelStore` |
| Checkpoints + dedupe | `CheckpointStore` | `InMemoryCheckpointStore` | `PostgresCheckpointStore` |
| Blue/green slot pointer | `SlotDirectory` | `InMemorySlotDirectory` | `PostgresCheckpointStore` (same row) |

The read model is a **namespaced key/value document store** (`{namespace: {key:
{value, version}}}`). Schemaless on purpose: every projection claims a namespace
and needs no migration of its own. The Postgres store backs it with the three
`esproj_*` tables (migration **`esproj_0001`**), which are deliberately *not*
foreign-keyed to the log or to `books`/`users` — read models are derived,
rebuildable state that must survive source deletions and be truncatable
independently (the conftest TRUNCATE-all isolation relies on that).

---

## Temporal / as-of reads (§8.5)

[`temporal.py`](temporal.py) reconstructs *any* projection's view as it stood at
a past point by folding the log prefix into a **throwaway** in-memory store
(never touching the live read model):

* `at_position(position)` — the view after the first N events (the "scroll back" cursor).
* `at_time(as_of)` — the view as the system believed it at instant T (`recorded_at`).
* `diff_rows(before, after)` — what changed between two as-of points (the
  "what did this director edit actually change" panel).

This makes §8.5 forgetting literal: a fact retired at beat 30 is `retired: True`
in the head view but `retired: False` in an as-of read before the retire — the
stale truth is preserved for time-travel reads yet invisible to the current view.

---

## Eventual consistency: lag + read-your-writes

[`lag.py`](lag.py): `LagTracker` snapshots how far each projection is behind the
store head (positions; `worst_lag` is the single SLA number). For
**read-your-writes**, a command hands back a `ConsistencyToken` carrying the
`global_position` its write landed at; a subsequent read calls
`has_caught_up(token)` (or awaits `wait_for(token)`) so a client never reads a
view missing its own write.

---

## Blue-green rebuilds

[`bluegreen.py`](bluegreen.py): each projection owns two namespaces (`::blue` /
`::green`). A rebuild replays into the **standby** slot while the **active** slot
keeps serving reads, then flips the `SlotDirectory` pointer atomically. The old
slot is retained for instant rollback (or cleared with `clear_old=True`). Each
slot checkpoints under an independent `<name>::<colour>` key. Reads always resolve
through `active_namespace(name)`, so a reader never knows which colour is live.
In-place `ProjectionRuntime.rebuild()` is the simpler offline alternative.

---

## Snapshots — replay acceleration

[`snapshots.py`](snapshots.py): a `Snapshot` captures a projection's whole
namespace at a `global_position`, so `ProjectionRuntime.restore_or_rebuild()` can
**restore the snapshot and replay only the tail** instead of the entire log —
O(events-since-snapshot) rather than O(all). `SnapshotPolicy(interval=N)` makes
the runtime snapshot opportunistically every N applied events during catch-up;
`snapshot_now()` is an explicit "snapshot before deploy" hook. Snapshots are an
**optimisation, never truth** — a snapshot taken under an older fold `version` is
ignored (the fold changed) and a missing one just costs a full replay.
`SnapshotStore` is the seam (`InMemorySnapshotStore` is the fake).

## Projection version guard — auto-rebuild on fold change

[`versioning.py`](versioning.py): a projection declares a `version`; bumping it
signals the fold changed incompatibly (replay alone can't fix rows written by the
old code — a rebuild must). `VersionGuard.ensure_current(projection, rebuild)`
compares the code `version` against the version stamped on the checkpoint and
rebuilds **only** when they differ, then re-stamps — turning "remember to rebuild
after editing the fold" into a checked invariant. `check_version` is the pure
decision function.

## Read facade

[`reader.py`](reader.py): `ProjectionReader` is the single read entry-point an API
route calls. It resolves a projection name to the namespace currently serving
reads (blue/green aware), optionally enforces read-your-writes against a
`ConsistencyToken` (returning a `ReadResult.stale` flag on timeout rather than
blocking forever), and returns the rows — so a route never touches slots,
checkpoints, or lag directly.

---

## Example projections (`examples/`)

| Projection | Folds | Read model |
|---|---|---|
| `session_timeline` | `session.*` events | one row per reading session: pages, deepest page, shots played, director comments, stalls, duration |
| `shot_status_board` | `shot.*` (§9.7) | one row per shot (status, attempts, QA score, mode) + a `__summary__` count row |
| `canon_audit_view` | `canon.*` (§7.2/§8) | one row per canon subject: current value, validity interval, retired flag, bounded mutation history |

---

## Composition

[`registry.py`](registry.py): `ProjectionRegistry` wires every projection to one
set of stores and mints runtimes, the lag tracker, the as-of projector, and the
blue-green rebuilder. `ProjectionSupervisor` runs many live tails as supervised
asyncio tasks (a fault in one does not stop the others). `default_projections()`
returns the three examples. A composition root would build the registry with the
Postgres stores and launch the supervisor in the `api` (or a dedicated projection
worker) process.

---

## Design principles (mirrors `app/db/DESIGN.md` + `app/analytics/DESIGN.md`)

1. **Lazy & side-effect-free imports.** Importing any module opens no sockets.
2. **Protocol seams + deterministic in-memory fakes.** The fakes are the test
   substrate and a viable tiny embedded backend; Postgres is one implementation.
3. **Everything typed; mypy clean** (`disallow_untyped_defs = true`).
4. **Additive only on shared files.** The single shared-file change is registering
   the three `esproj_*` models on `Base.metadata` in `app/db/models/__init__.py`
   (so Alembic autogenerate + `create_all` see them). No existing table touched.

---

## Roadmap / status

| Phase | Scope | Status |
|---|---|---|
| 1 | Contract, projection base, in-memory stores, runtime (catch-up + live tail + at-least-once + idempotent + retry/dead-letter), lag + RYW, temporal/as-of, blue-green, 3 example projections, registry/supervisor, Postgres stores + `esproj_0001` | **done** |
| 2 | Position-stamped **snapshots** (skip full replay on rebuild), projection **version guard** (auto-rebuild on incompatible fold change), a read-API facade | **done** |
| 3 | Lag **SLA classifier** + health rollup; **catch-up metrics**; dead-letter store | planned |

The DB-backed half is covered by `tests/test_es_projections_pg.py` (SKIPS without
`KINORA_TEST_DATABASE_URL`); the logic is fully covered by the in-memory suites.
