# `app/distributed/sagas` — saga / process-manager engine (distributed-systems facet C)

> Owner: distributed-systems facet-C agent. Self-contained NEW package under
> `backend/app/distributed/sagas/`. **Additive** to the repo: it adds three DB
> tables and one import line to a shared registry; it changes no existing
> behaviour. Sibling facets (replication, rpc) live alongside it under
> `app/distributed/`.

Durable **cross-service coordination** for the Kinora backend. Once a unit of work
spans services that cannot share one ACID transaction — *stage a PDF, extract
pages, build the canon, lock identity, mark ready*; or *reserve budget, render,
QA, arbitrate, accept* — a single `try/except` is not enough: a crash mid-way
leaves the system half-applied. This package gives those flows the discipline
kinora.md §12 demands (idempotent, retried, dead-lettered, observable) in the form
of **sagas**: a sequence of steps, each with a compensation, run durably so a crash
resumes and a failure rolls back.

It reads kinora.md **§7.2** (the Showrunner arbitration policy, modelled as a pure
function in the render flow), **§9.7** (the per-shot `Rendering → QA → Repair →
{regen, Conflict, Degraded}` state machine, modelled as the render saga), and
**§12** (the render queue's idempotency-key / retry / DLQ engineering, generalised
here to arbitrary multi-step flows).

## Why a saga engine (not the render queue or the jobs framework)

- The **render queue** (`app/queue`, §12.1) is a *priority queue of one-shot shot
  renders* — lanes, preemption, `shot_hash` idempotency. One job, no compensation.
- The **jobs framework** (`app/jobs`) is a *time/trigger framework of operational
  tasks* — cron/interval, a leader lease, one durable run per trigger.
- A **saga** is a *multi-step business transaction with rollback*: an ordered list
  of `step → compensation` pairs, a shared state bag threaded through the steps,
  backward recovery on failure, and crash-resume from a durable cursor. Different
  shape, different concerns — its own package so none of the three leak into the
  others. It deliberately **reuses** `app.jobs.clock` (the `Clock` / `ManualClock`
  virtual-time source) so the whole backend tests coordination in virtual time.

## Two coordination styles, one set of correctness primitives

- **Orchestration** (`orchestrator.py`): a central `SagaOrchestrator` drives a
  `SagaDefinition` through a durable `SagaStore`, persisting at *every* transition.
- **Choreography** (`choreography.py`): no central driver — services react to
  events on a durable `EventBus` and emit the next event; a `ProcessManager`
  correlates the stream and keeps reactions exactly-once.

Shared machinery both rely on:

- **Exactly-once effects** (`effects.py`): an `EffectLedger` turns at-least-once
  execution into exactly-once side effects via stable idempotency keys. A step
  wraps every non-idempotent action in `ctx.effects.once(key, action)`; a retry or
  crash-resume returns the recorded result instead of re-running. If the action
  *raises* (a transient fault) the claim is released so the in-process retry
  re-runs; a true cross-process crash leaves a `PENDING` claim for the reaper.
- **Distributed locks/leases with fencing** (`locks.py`): monotonic fencing tokens
  that a `FencedResource` enforces, so a stalled old lease-holder's write is
  rejected (Kleppmann's fencing).
- **Timeouts / retries / dead-letter**: per-step `BackoffPolicy` (forward +
  compensation budgets), a per-invocation timeout, an overall saga `deadline`, and
  a terminal `FAILED` state that is the engine's dead-letter (a compensation that
  could not complete — surfaced loudly, never silently retried).

## Components (modules)

| Module | Responsibility |
|---|---|
| `types.py` | `SagaStatus` / `StepStatus` / `StepDirection` / `SagaOutcome` enums; `SagaInstance` / `StepRecord` value types; `SagaContext`; `StepResult`; `StepFailed`. |
| `backoff.py` | Pure retry policy (exponential + jitter ceiling); separate forward + compensation defaults. |
| `effects.py` | `EffectLedger` protocol + `InMemoryEffectLedger` / `RedisEffectLedger`; the exactly-once core. |
| `locks.py` | `LockManager` protocol + `InMemoryLockManager` / `RedisLockManager`; `Lease`, `FencedResource` (monotonic fencing). |
| `definition.py` | `SagaStep` / `SagaDefinition` + `saga()` / `step()` builders + `SagaRegistry`. |
| `store.py` | `SagaStore` protocol + `InMemorySagaStore`; atomic start (dedup) / claim (lease) / save / load / reap. |
| `orchestrator.py` | The engine: forward progress, reverse compensation, crash-resume, deadline + retry handling. |
| `runner.py` | `SagaWorker` loop: claim → drive → release + reap (crash recovery across workers). |
| `choreography.py` | `ChoreographyEvent` / `EventBus` / `InMemoryEventBus` / `ProcessManager` (event-driven mode). |
| `metrics.py` | Dependency-free in-process counters for every saga/step transition. |
| `models.py` (DB) | `saga_instances` + `saga_steps` + `saga_effects` ORM (additive) for the durable backends. |
| `db_store.py` | `PostgresSagaStore` + `PostgresEffectLedger` over the new tables (durable, queryable). |
| `flows/` | The two concrete Kinora sagas + their service ports + in-memory fakes. |

## The two concrete flows (`flows/`)

- `flows/ingest.py` — **ingest → canon-build → identity-lock**. Five compensatable
  steps (stage source → extract pages → build canon → lock identity → mark ready),
  each with an undo. Depends only on the `IngestPorts` protocol.
- `flows/render.py` — **render → QA → conflict → degrade** (§9.7 + §7.2 + §12.4).
  cache-check → reserve budget → render → QA/arbitrate → accept. The QA step owns
  the §9.7 regeneration loop and applies the §7.2 `arbitrate()` policy
  (evolve / surface-to-user / honor); on exhausted QA retries it drops to the
  §12.4 Ken-Burns ladder rather than failing — the film never hard-stops. A render
  that fails past its budget rolls back, releasing the budget reservation (never a
  double-spend). Depends only on the `RenderPorts` protocol.
- `flows/fakes.py` — in-memory `FakeIngestServices` / `FakeRenderServices` with
  fault injection; **zero credits**, `KINORA_LIVE_VIDEO` irrelevant. The production
  adapters over the real services are wired via the orchestrator `resources` bag.

## Durability & crash-resume

The `InMemorySagaStore` is "durable within a process" — exactly what the
deterministic tests need: a *crash* is dropping the `SagaOrchestrator` object while
keeping the store; a *resume* is constructing a fresh orchestrator over the same
store and calling `resume()`. The `PostgresSagaStore` carries production with two
database-level guarantees mirroring the jobs framework:

- **Idempotent start** — a partial unique index on
  `saga_instances(definition, correlation_id)` for *active* statuses, so a
  re-delivered start collides and dedups.
- **Exclusive claim** — `SELECT … FOR UPDATE SKIP LOCKED` in `claim_due`, so
  concurrent workers never drive the same saga.

The `PostgresEffectLedger` gives the same exactly-once guarantee via a UNIQUE
`saga_effects.key`.

## Tests (deterministic virtual clock; no infra required for the core)

`tests/distributed/sagas/` — 60 tests. The core (orchestrator, crash-resume,
effects, locks, runner, choreography, both flows, the Redis backends over a tiny
in-process fake) runs with **no infrastructure** on the `ManualClock`, so every
backoff/deadline is resolved in virtual time and the proofs are exact. The
Postgres store/ledger tests are gated on `KINORA_TEST_DATABASE_URL` (the isolated
`sagas_test` DB on :5433) and skip cleanly when it is unset.

Headline proofs: forward-commit + reverse-compensation ordering; retries→compensate
and compensation-exhausted→FAILED; **crash-resume does not double-apply a
ledger-wrapped effect**; deadline pre-empts a long backoff; fencing rejects a
stalled holder; the render saga degrades to Ken-Burns on persistent conflict and
releases budget on infra failure.

## Additive shared-file changes (the only edits outside this package)

1. **`app/db/models/__init__.py`** — one import block registering
   `SagaInstanceRow` / `SagaStepRow` / `SagaEffectRow` on `Base.metadata` (and the
   three names added to `__all__`), exactly mirroring how `app.flags` / `app.media`
   register their own tables. Side-effect-only style; touches no other model.
2. **`migrations/versions/sagas_0001_saga_engine_tables.py`** — a NEW Alembic
   migration (revision id `sagas_0001`, `down_revision = a1b2c3d4e5f6`, the shared
   base the sibling subsystem migrations are cut from) creating the three tables +
   their indexes (incl. the partial unique index behind idempotent start). Touches
   no existing table; reconciled with the other sibling heads at merge time.

Nothing else outside `app/distributed/` and `tests/distributed/` is modified.

## Production wiring (composition seam)

`build_saga_engine(...)` in `composition.py` assembles a ready `SagaOrchestrator`
+ `SagaWorker` over either the in-memory backends (default) or the Postgres/Redis
backends when a session factory / Redis handle is supplied — the single seam the
real backend would call to register the ingest/render flows and drain them.
