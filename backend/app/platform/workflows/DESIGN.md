# `app/platform/workflows` — durable-execution (Temporal-style) engine

> Owner: platform-engineering agent (facet B). Self-contained NEW package under
> `backend/app/platform/workflows/`. **Composes on top of — never edits —** the
> operational jobs framework (`app/jobs`, whose `Clock` it reuses) and the shot
> render queue (`app/queue`). Additive-only on shared files (documented below).

A **durable-execution runtime**: workflows are deterministic, replayable code over
an **event-history + command** model, so a crashed run resumes bit-for-bit
identical to one that never crashed. On that primitive sit durable timers, signals
& queries, activities (at-least-once + heartbeating + retries + timeouts), child
workflows, continue-as-new, and versioning/patching — plus a worker runtime, task
queues, and two concrete production workflows. It reads kinora.md §9.7 (the per-shot
state machine the ingest/render workflow makes durable) and §12 (the engineering
discipline: idempotent, retried, dead-lettered, observable).

## Why a *third* framework (not the jobs framework, not the render queue)

| Subsystem | Shape | Unit | Owns |
|---|---|---|---|
| `app/queue` (§12.1) | priority queue | one **shot render** | lanes, preemption, budget reservation, `shot_hash` dedup |
| `app/jobs` | time/trigger framework | one **operational task** | cron/interval triggers, leader lease, retries+DLQ |
| `app/platform/workflows` (**this**) | durable-execution runtime | one **multi-step workflow** | event-sourced replay, timers/signals/queries, child workflows, versioning |

A durable workflow *orchestrates* the other two (its activities can enqueue a
render job or kick a jobs-framework task); it is a strictly higher-order layer.

## The model in one paragraph

A workflow is an `async def` whose every interaction with the outside world goes
through a `WorkflowContext` and is recorded as a `HistoryEvent`. The body emits
**commands** (`ScheduleActivity`, `StartTimer`, …); the **executor** matches them,
by sequence number, against the recorded events (a mismatch is a
`NonDeterminismError`) and turns the *new* commands into appended events + dispatch
instructions. The **worker** runtime turns dispatches into real work: it runs
activities (at-least-once, leased, heartbeated, retried, timed-out) and, on each
activity/timer/child completion, appends the completion event and re-enqueues a
workflow task. The workflow is re-run from the top on every task; futures resolve
from history, so it deterministically reproduces its prior path and then advances.

## Components (modules)

| Module | Responsibility |
|---|---|
| `errors.py` | Exception hierarchy (`NonDeterminismError`, `ActivityFailure/Timeout/Cancelled`, `ApplicationError`, …). |
| `serde.py` | Canonical, lossless JSON serialisation of payloads (deterministic, sorted keys). |
| `events.py` | `EventType` + `HistoryEvent`: the append-only event-history value type. |
| `commands.py` | The commands a workflow emits during a task. |
| `retry.py` | `RetryPolicy` — deterministic, JSON-serialisable activity backoff. |
| `determinism.py` | Run-seeded RNG, deterministic UUIDs, replayed `now()`. |
| `futures.py` | `WorkflowFuture` (suspends via `WorkflowSuspended`) + `gather`/`wait_any`. |
| `versioning.py` | `get_version`/`patched` — safe deploys against in-flight histories. |
| `context.py` | `WorkflowContext`: the durable, deterministic API handed to workflow code. |
| `replay.py` | `ReplayState` + history indexing + the coroutine stepper. |
| `executor.py` | `execute_workflow_task` — replay → match (non-determinism) → append + dispatch. |
| `registry.py` | `@workflow`/`@activity` decorators + name→definition registries. |
| `activity.py` | `ActivityContext` (heartbeat/checkpoint/identity) handed to activities. |
| `heartbeat.py` | `Heartbeater` — lease renewal + progress checkpointing. |
| `store.py` | `WorkflowStore` protocol + value types (execution/tasks/timers). |
| `memory_store.py` | `InMemoryWorkflowStore` — zero-infra, full contract (tests + harness). |
| `db_store.py` | `PostgresWorkflowStore` — durable backend over the `workflow_*` tables. |
| `worker.py` | `WorkflowTaskProcessor` + `ActivityTaskProcessor` + `TimerService` + `Worker`. |
| `client.py` | `WorkflowClient` — start/signal/query/cancel/describe (the outside-world handle). |
| `service.py` | `WorkflowService` facade wiring store+registries+client+worker. |
| `harness.py` | `WorkflowTestEnvironment` + `assert_deterministic_replay` (virtual-clock driver). |
| `metrics.py` | `kinora_workflow_*` Prometheus series on the shared registry. |
| `ids.py` | Opaque, prefixed ids for engine-internal records (run/task/timer). |
| `__main__.py` | Opt-in `python -m app.platform.workflows` worker entrypoint. |
| `defs/ingest_render.py` | Concrete: book-ingest → render-whole-scene (§9.7 per-shot state machine). |
| `defs/episode.py` | Concrete: multi-agent "produce an episode" (signals/queries/children/CAN). |

## Semantics / guarantees

- **Deterministic replay ≡ crash-resume.** The body is pure w.r.t. (inputs,
  history). Non-determinism sources (clock/RNG/UUID/local compute) are routed
  through the context and recorded (`now()` = the replayed event timestamp,
  `random()` = run-seeded, `uuid4()` = run+seq derived, `side_effect` = memoised).
  Proven by `assert_deterministic_replay` and the every-prefix + fresh-worker-
  per-step tests.
- **Seq-keyed command↔event matching.** Commands carry a workflow-local `seq`; the
  executor matches by seq (not position), so *memoised* commands (`side_effect`,
  `get_version`) that re-emit nothing on replay don't skew the stream. Version
  markers are keyed by `change_id` and draw **no** main-counter seq, keeping the
  activity/timer seq stream identical whether they record or read back.
- **At-least-once activities + exactly-once history effect.** Activity tasks are
  leased with a visibility timeout; a crashed worker's lease lapses → re-delivery
  (so activities must be idempotent — the §12.1 `shot_hash` discipline generalised).
  The workflow side is exactly-once because of optimistic-concurrency appends
  (`last_event_id` CAS, backed by `UNIQUE(workflow_id, run_id, event_id)`).
- **Timeouts + heartbeating.** Start-to-close timeout via `asyncio.wait_for`;
  heartbeats renew the lease and checkpoint progress so a retried attempt can
  resume. Retries follow the deterministic `RetryPolicy`; exhaustion surfaces a
  catchable `ActivityFailure` (compensation), never a silent stall.
- **Signals/queries/cancel.** Signals are external history events the workflow
  reacts to; queries replay read-only (no append); cancel is a
  `WORKFLOW_CANCEL_REQUESTED` event observed via `ctx.is_cancelled`.
- **Child workflows + continue-as-new.** Children are full executions linked to a
  parent (seq); completion notifies the parent. Continue-as-new closes a run and
  starts a fresh one with a compact history (bounded history for book-length runs).
- **Versioning/patching.** `get_version(change_id, min, max)` pins `max` for fresh
  runs (recorded), returns the recorded value on replay, and returns
  `DEFAULT_VERSION` for old in-flight runs that predate the change (frontier
  detection) — so new code deploys safely against old histories.

## Additive shared-file changes (documented per the rules)

- `app/db/models/__init__.py` — **DONE (additive only):** import + export the five
  ORM rows (`WorkflowExecutionRow`, `WorkflowEventRow`, `WorkflowTaskRow`,
  `WorkflowActivityTaskRow`, `WorkflowTimerRow`). No existing line changed.
- New ORM module `app/db/models/workflow.py` (new file, registers the five tables).
- New Alembic migration `migrations/versions/workflows_0001_durable_workflow_engine_tables.py`
  — unique revision id **`workflows_0001`**, branches off the shared base
  `a1b2c3d4e5f6` in its own head (merges cleanly alongside the other parallel
  platform packages). Creates the five `workflow_*` tables + indexes; `downgrade`
  drops them. Touches no existing table/migration.
- `app/core/config.py`, `app/composition.py`, `app/main.py` — **untouched.** The
  engine is self-contained and exercised through `WorkflowService` / the harness;
  it never enters the composition root (avoids colliding with the ~29 parallel
  agents). The opt-in `python -m app.platform.workflows` entrypoint is how a
  deployment would run a worker; wiring it into the API lifespan / a compose
  `workflow-worker` service is a documented future step, not done here.

## Test infra

Isolated, mirroring the repo convention. The in-memory store + virtual-clock
harness make the *entire* engine (and both concrete workflows) testable with **zero
infra** — these are real contract tests, not mocks. The Postgres-durable store
tests gate on `KINORA_TEST_DATABASE_URL` (isolated test DB on :5433) and **skip
cleanly** when unset. `KINORA_LIVE_VIDEO` stays OFF; the concrete workflows' crew
activities are idempotent simulations (zero credits).

Test files (`tests/test_workflows_*.py`): `serde`, `determinism` (crash-resume ≡
fresh-run at every prefix + non-determinism detection), `activities`
(retry/timeout/heartbeat/cancel/non-retryable), `timers_signals`
(durable timer/signal/query/cancel/race), `composition` (child workflows +
continue-as-new), `versioning` (patch/get_version old-vs-new), `store`
(optimistic-concurrency + lease + dedup), `worker_crash_resume` (fresh-worker-per-
step ≡ long-lived + at-least-once redelivery), `concrete`
(ingest_render + episode end-to-end), `db_store` (Postgres-durable, infra-gated).

## Milestones

1. **M1 — Event-sourced core ✅** events/commands/serde/retry/determinism/futures.
2. **M2 — Replay engine ✅** context + replay + executor (seq-keyed matching, non-determinism detection).
3. **M3 — Registry + activities ✅** `@workflow`/`@activity`, activity context, heartbeating.
4. **M4 — Durable store ✅** protocol + in-memory + Postgres (`workflow_*` tables, migration `workflows_0001`).
5. **M5 — Worker runtime ✅** workflow/activity processors + timer service + `Worker` + task queues.
6. **M6 — Client + service + harness ✅** start/signal/query/cancel + `WorkflowService` + deterministic harness.
7. **M7 — Advanced features ✅** child workflows, continue-as-new, versioning/patching.
8. **M8 — Concrete workflows ✅** book-ingest→render-scene + produce-an-episode.
9. **M9 — Observability ✅** `kinora_workflow_*` Prometheus metrics.

## Remaining / future roadmap

- **Wire the real services into the concrete activities.** Each activity in
  `defs/` is an idempotent simulation today; replacing the body with the real
  `app.ingest`/`app.render`/`app.agents` call is a localized change that leaves the
  durable orchestration untouched. (Keep `KINORA_LIVE_VIDEO` off until intended.)
- **Composition wiring.** Start a `WorkflowService` in the API lifespan (behind a
  settings flag) or add a `workflow-worker` compose service running
  `python -m app.platform.workflows`; inject a real session factory.
- **API admin surface.** List/describe executions, signal/query/cancel, replay a
  history — `WorkflowClient`/`WorkflowService` already expose every operation.
- **Schedule-to-close timeout enforcement** + a separate cron-style "start a
  workflow on a schedule" bridge to `app/jobs` (a jobs handler that calls
  `client.start_workflow`).
- **Redis-backed store** (a `RedisWorkflowStore`) for lower-latency task dispatch,
  mirroring `app/jobs/redis_store.py`; the contract is already abstracted.
- **History sealing / archival** of terminal runs to object storage.
- **Search attributes + visibility queries** over `list_executions`.
```
