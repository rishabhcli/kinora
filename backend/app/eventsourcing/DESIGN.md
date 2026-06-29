# `app.eventsourcing` — Event Sourcing core

Kinora's reading sessions, the §9.7 render-shot lifecycle, and the §5.4 canon-edit
flow are inherently **event-shaped**: they are sequences of facts (`SessionStarted`,
`ShotRendered`, `CanonFieldEdited`) whose *order* and *audit trail* matter, and
which several subsystems (the scheduler, the budget accounting, the director-tools
UI, preference write-back) need to react to. This package is a CQRS / Event Sourcing
core for that write path — built **additively, on seams**, so it can sit alongside
the existing SQLAlchemy row model rather than replacing it.

It is split into two facets owned by two agents:

| Facet | Package | Owns |
|---|---|---|
| **A — event store** | `app/eventsourcing/store/` | the append-only persistence seam: the `EventStore` protocol, the optimistic-concurrency contract, the persisted-record shape, concrete adapters (in-memory now; Postgres/snapshot later), and the `SnapshotStore` seam. |
| **B — command + aggregate model** | `app/eventsourcing/domain/` | the CQRS **write side**: the command bus + middleware, aggregate roots, the event/upcaster framework, the three domain aggregates, optimistic-concurrency retries, the saga-trigger seam, and the read-model projection seam. |

**This document and the code under `domain/` are facet B.** Facet A is a sibling
agent's domain; until it lands on disk, facet B ships a *minimal but complete*
definition of the `EventStore` / `SnapshotStore` protocols plus in-memory
reference implementations under `store/`, so the write side is fully testable in
isolation. See [The facet-A seam](#the-facet-a-seam) for the exact contract facet
A must satisfy.

Everything here is **pure and import-safe**: no sockets, no DB, no event loop at
import time. Persistence is reached only through the injected protocols.

---

## The write side at a glance

```
Command ──► CommandBus.dispatch
              │  (middleware onion, outermost first)
              ├─ LoggingMiddleware
              ├─ ValidationMiddleware      structural validation (pre-load)
              ├─ AuthorizationMiddleware   the auth seam (pluggable policy)
              ├─ IdempotencyMiddleware     dedupe retried submissions
              └─ handler ──► Repository.load(id)        (snapshot + tail replay)
                              │
                              ▼
                          Aggregate.decide()  ── pure: validate invariants, emit events
                              │
                              ▼
                  retry_on_conflict( Repository.save_with_metadata )   optimistic concurrency
                              │  (on ConcurrencyError: re-load, re-decide, re-append)
                              ▼
                      EventStore.append(stream, envelopes, expected_version)
                              │  committed
              ┌───────────────┴───────────────┐
              ▼                                ▼
      SagaDispatcher                    ProjectionSink
   (fact ─► next command)        (fact ─► read-model fold)   ← CQRS query side
```

### Core building blocks (`domain/`)

| Module | Responsibility |
|---|---|
| `events.py` | `DomainEvent` base + the envelope `{type, version, data, meta}`, `EventMetadata` (causation/correlation/actor), the `EventRegistry`, and `serialise`/`deserialise`. |
| `upcasting.py` | event **versioning**: step-wise `data(vN) → data(vN+1)` upcasters, chained on load. History is never rewritten. |
| `identifiers.py` | typed `StreamId` (`StreamCategory` + aggregate id) → the opaque `"{category}-{id}"` the store keys on. |
| `aggregate.py` | `AggregateRoot`: *decide → emit* (`emit` folds + queues) and *rebuild from history* (`replay`); version bookkeeping; the optimistic-concurrency token (`expected_version`). |
| `commands.py` / `commands_catalog.py` | the `Command` base + every concrete command. |
| `validators.py` | structural per-command validators (run pre-load). |
| `middleware.py` | the bus pipeline: validation, the **auth seam** (`AuthPolicy`), idempotency, logging. |
| `concurrency.py` | the `RetryPolicy` + `retry_on_conflict` loop (re-load + re-decide on conflict). |
| `bus.py` | `CommandBus`: route → middleware → retry → stamp metadata → append → sagas + projections. |
| `repository.py` | the only bridge to the store: load (snapshot + tail), save (optimistic), snapshot-on-policy. |
| `saga.py` / `sagas_catalog.py` | the **saga-trigger seam**: committed fact → follow-up command(s). |
| `snapshotting.py` | the `Snapshotter` aggregate protocol + `SnapshotPolicy` + total coercion helpers. |
| `projection.py` | the **read-model projection seam** (CQRS query side) + reference read models. |
| `wiring.py` | `build_command_bus(store, …)` — assembles a fully-wired bus over an injected store. |

### The three aggregates

- **`session.py` — `SessionAggregate`** (§5.2–§5.4, §9.6). The SyncEngine/client
  lifecycle: `SessionStarted → IntentUpdated* → ModeSwitched* → DirectorCommentLeft* →
  PreferenceRecorded* → SessionEnded`. Intent updates emit only on a *material* move
  (sub-epsilon nudges are absorbed) so the stream is an audit trail, not a scroll
  firehose. Comments are Director-mode-only and target a shot (§5.4 REST regen path).
- **`render_shot.py` — `RenderShotAggregate`** (§9.7). The per-shot state machine as
  an event stream. It **reuses `app.render.states.ALLOWED_TRANSITIONS`** verbatim so
  the event-sourced model can never drift from the in-pipeline `ShotStateMachine`.
  Enforces the §9.7 `≤ 2` repair cap as an aggregate invariant and accumulates the
  video-seconds spent (§11.1). The §5.4 director/canon **re-do** is a distinct
  `ShotRegenRequested` that re-opens a settled/in-flight shot to `Promoted` with a
  fresh attempt budget — deliberately *not* a §9.7 edge, since it is a cross-cutting
  lifecycle event rather than one of the diagram's intra-attempt transitions.
- **`canon.py` — `CanonEntityAggregate`** (§5.4, §8). One canon entity's edit stream
  with a **monotonic `canon_version`** (the read token agents check for staleness)
  and a domain-level lost-update guard (`expected_canon_version`). Each edit captures
  the `dependent_shot_ids` so a saga regenerates exactly those — *surgical, not a full
  re-render*.

### The §9.7 / §5.4 sagas (`sagas_catalog.py`)

- `DirectorCommentLeft` → `RegenerateShot(target)` — the §5.4 REST regen path.
- `CanonFieldEdited` / `CanonReferenceImageSwapped` / `CanonEvolvedFromConflict` →
  `RegenerateShot` per dependent shot — §5.4 surgical regeneration.

The dispatcher only *decides* the follow-up commands and carries forward a
causation chain (`causation_id` = the triggering event's id, same `correlation_id`).
**Whether they run inline (same bus) or are enqueued is the composition root's
choice** — wire the dispatcher's `SagaSink`. The domain never owns the
scheduler/queue. (The QA-fail → Accept/Repair routing is a single atomic decision in
`handle_score_qa`, not a saga hop, so it is exactly-once with the QA event.)

### CQRS query side (`projection.py`)

Read models are derived by folding the committed stream. `ProjectionManager` fans
events to registered `Projection`s either **inline** (the bus's `projection_sink`,
updating read models in lockstep with each command) or **catch-up** (`project_stored`
over the store's global-ordered events, for a background rebuild). Reference read
models: `ShotStatusProjection` (per-shot status + `total_video_seconds`, the §5.4
timeline + §11.1 budget source) and `SessionListProjection` (per-user live sessions).

---

## The facet-A seam

Facet B depends on **only** these protocols from `store/` (it never reaches past
them). Facet A must satisfy them; it is free to *widen* with concrete adapters
(Postgres, snapshotting), but nothing in facet B may depend on anything narrower.

### `EventStore` (`store/protocol.py`)

```python
async def append(stream_id, events, *, expected_version) -> AppendResult
async def load(stream_id, *, from_version=0) -> Sequence[StoredEvent]
async def current_version(stream_id) -> int
```

- **Optimistic concurrency.** `expected_version` is the version the writer believed
  the stream was at. On mismatch, raise `ConcurrencyError` and write **nothing**
  (atomic). `expected_version=0` asserts "stream does not exist yet"; `None` skips the
  check. The first event in a stream is version 1; the stream's current version is its
  last event's version (0 when empty).
- **Envelope shape.** Each appended element is `{"type", "version", "data", "meta"}`
  (the output of `domain.events.serialise`). `load` returns `StoredEvent`s carrying the
  same blocks plus the assigned `version` and an optional store-wide `global_position`.
- **`from_version`** is exclusive (`> from_version`), used to replay on top of a snapshot.

### `SnapshotStore` (`store/snapshots.py`)

```python
async def save(snapshot: Snapshot) -> None        # keep the highest version
async def load(stream_id: str) -> Snapshot | None
```

A `Snapshot` is `(stream_id, version, state)` where `state` is the aggregate's
`snapshot_state()` mapping. The repository, when given a snapshot store and a
`Snapshotter` aggregate, restores the snapshot then replays only events after it.
Snapshot loads must be **observationally identical** to a full replay (tested).

### Reference implementations (provided by facet B as a fallback)

`store/memory.py::InMemoryEventStore` and `store/snapshots.py::InMemorySnapshotStore`
are correct, faithful in-memory implementations used by the write-side tests. When
facet A lands, it should keep these (or equivalents) available for unit tests. If
facet A's `store/` package already exists on disk, **prefer its protocol** and treat
the definitions here as the compatibility baseline.

---

## Additive shared-file changes

This package is **purely additive** — it adds the new `app/eventsourcing/` tree and
nothing else. As of this writing it touches **no** pre-existing shared file:

- It **reads** `app.render.states` (`ALLOWED_TRANSITIONS`, `RenderState`, `is_allowed`,
  `to_status`) and `app.db.models.enums` (`SessionMode`, `ShotStatus`, `EntityType`)
  — imports only, no edits — so the event-sourced model stays in lockstep with the
  existing §9.7 machine and enum vocabulary.
- It is **not** wired into `app/composition.py`, any route, or `app/main.py`. A future
  integration step would construct a `build_command_bus(...)` in the composition root
  (injecting facet A's store + the RBAC-backed `AuthPolicy` + a `SagaSink` that
  enqueues onto the existing Redis render queue) — but that is a separate, deliberate
  change, not part of this package.
- **No new tables / no Alembic migration.** Persistence lives behind facet A's store
  protocol; facet B adds no schema. (If facet B ever needed its own table it would use
  a unique `es_b_*`-prefixed revision id — but it does not.)

## Testing

Pure, no infra. `backend/.venv/bin/pytest tests/test_eventsourcing_*.py -q` covers
the event/upcaster framework, the store contract, the aggregate base + repository,
each aggregate's decision functions exhaustively (including every §9.7 edge, the
illegal-edge guard, and the retry cap), the middleware in isolation, the
concurrency-retry schedule, the wired bus end-to-end (including the optimistic-retry
recovery under a racing writer), the sagas (inline-sink end-to-end), snapshots
(round-trip + snapshot-equals-full-replay), and the projections (inline + catch-up).
`make lint` (ruff + mypy) is green for the whole package and its tests.
