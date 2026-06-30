# `app/sagas/` — durable saga / workflow engine

A small, dependency-free workflow engine that makes Kinora's multi-step,
side-effecting pipelines — **ingest** (parse → keyframes → identity → canon) and
the **§9.7 per-shot render** (reserve → design → generate → normalize → persist →
QA) — durable, resumable, replay-idempotent, and self-undoing.

This package is **additive and self-contained**: it lives entirely under
`app.sagas.*`, imports only `app.core.logging` / `app.core.config`, and changes
no existing behaviour. Per the FINAL-ROUND isolation rule it defines its own
local primitives (e.g. `clock.py`, mirroring `app.cache.clock`) rather than
depending on a sibling subsystem.

## Why

A naive re-run of an interrupted pipeline is dangerous: it double-spends
video-seconds (the budget), re-writes object storage, or leaves a book stuck
half-imported. The engine guarantees:

* **crash-resume** — state persists after every step; a restart continues from
  the last completed step, not the top;
* **deterministic replay / idempotency** — a completed step replays its recorded
  result and is never re-executed; an attempt-invariant idempotency key lets a
  side effect re-driven across a crash dedupe (same history ⇒ same path);
* **saga compensation** — on a failure past the point of no return, completed
  steps' compensations run **in reverse**, best-effort and recorded;
* **timers + signals + recovery** — steps await external events with a timeout
  that routes to a branch; a sweep fires due timers and re-claims abandoned runs.

## Module map

| Module | Responsibility |
|---|---|
| `clock.py` | `Clock` protocol + `SystemClock` / `FakeClock` (deterministic time). |
| `ids.py` | `new_run_id`, attempt-invariant `step_idempotency_key`, `fingerprint`. |
| `errors.py` | exception taxonomy: transient vs permanent step errors; `SagaFailed`. |
| `policy.py` | `RetryPolicy` (deterministic exponential backoff + seeded jitter), `TimeoutPolicy`. |
| `history.py` | event-sourced, JSON-round-trippable `RunState` / `StepRecord` (pydantic v2). |
| `definition.py` | the DSL: `Step` (action + compensation + retry + timeout + branch + await), `Workflow`, `WorkflowBuilder`. |
| `registry.py` | name → `Workflow` (a run persists only the name). |
| `store.py` | `DurableStore` protocol + `InMemoryDurableStore` (optimistic concurrency, due/stuck queries). |
| `context.py` | `StepContext` — a step's view: input, idempotency key, durable shared state, signal payload, clock. |
| `engine.py` | `SagaEngine` — start / resume / signal / cancel / fire_due_timers; the driver, retry+timeout race, branching, compensation. |
| `recovery.py` | `RecoverySweeper` — fire due timers + re-claim expired-lease runs. |
| `telemetry.py` | `TelemetryBus` / `RecordingBus` — lifecycle events for metrics + test assertions. |
| `composition.py` | `build_saga_runtime` — wire registry + store + engine + sweeper. |
| `workflows/ingest.py` | the ingest pipeline saga over an injected `IngestPort`. |
| `workflows/render_shot.py` | the §9.7 per-shot render saga over an injected `RenderPort`, with the §12.4 degrade branch. |

## Execution model

A `Workflow` is an ordered list of `Step`s. The engine keeps a `cursor` into that
list in the durable `RunState`. For each step it:

1. **replays** if already `COMPLETED` (emits `step_skipped`, never re-runs);
2. **awaits** a signal if declared — parks `WAITING` (persisted) until
   `signal()` or a timer fires; an await-timeout routes to `on_await_timeout`;
3. **executes** the action under the retry+timeout policy, computing a stable
   idempotency key from `(run_id, step, input, salt)` and persisting it *before*
   the call so a crash-resume reproduces the same key;
4. on success records the result and advances the cursor (honouring `branch`);
5. on failure past retries raises an internal `_CompensateRun`, flipping the run
   to `COMPENSATING` and unwinding completed steps' compensations in reverse.

Backoff and timeouts go through the injected `Clock` + sleeper, never wall time,
so a `FakeClock` makes every retry/timeout/recovery decision deterministic.

## Determinism / testability

The engine reads time through `Clock`, sleeps through an injected sleeper, mints
run ids through an injected factory, and performs side effects only through
injected actions. Tests therefore run the full matrix — persistence, crash-resume,
replay idempotency, reverse compensation, retry/timeout, stuck-run recovery,
branching, signal await — entirely in-memory with a `FakeClock`, no infra and no
network, and never touch `KINORA_LIVE_VIDEO` or spend.

## Production wiring (not done here — additive seam only)

`build_saga_runtime` returns a `SagaRuntime`. A production composition root would
pass a DB/Redis-backed `DurableStore`, the real `SystemClock`, `asyncio.sleep`,
and implementations of `IngestPort` / `RenderPort` over the existing ingest /
render services; the API's idle-sweeper would call `RecoverySweeper.sweep` on
`saga_recovery_interval_s`. None of that is enabled in this change.
