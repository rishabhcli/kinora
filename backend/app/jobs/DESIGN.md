# `app/jobs` — general scheduled-jobs & background-task framework

> Owner: background-jobs agent. Self-contained package under `backend/app/jobs/`.
> **Distinct from** the shot render queue (`app/queue`) and the in-process
> Scheduler / idle-sweeper (`app/scheduler`, `app/main`). Those stay untouched.

This is the durable, distributed framework for *operational* background work the
rest of the system needs on a cadence — digest flushes, search-index refreshes,
retention/GC sweeps, stuck-import recovery cadence, budget reconciliation — plus
one-off enqueued tasks. It reads kinora.md §4.7 (idle sweeper — a sibling cadence
loop, the pattern we generalize) and §12 (the engineering discipline: idempotent,
retried, dead-lettered, observable).

## Why a second framework (not the render queue)

The render queue (§12.1) is a **priority** queue of *shot renders*: lanes,
preemption, budget reservation, cancel tokens keyed on `shot_hash`. The jobs
framework is a **time/trigger** framework of *operational tasks*: cron/interval
triggers, a distributed leader lease so a periodic job fires on exactly one node,
a typed registry/decorator, retries+backoff+DLQ on a durable store, and a
deterministic virtual clock for tests. Different shape, different concerns — kept
in its own package so neither leaks into the other.

## Components (modules)

| Module | Responsibility |
|---|---|
| `clock.py` | `Clock` protocol; `SystemClock`; `ManualClock` (deterministic virtual time). |
| `types.py` | `JobStatus`, `TriggerKind`, `RunOutcome` enums + value types / context / result. |
| `cron.py` | Minimal 5-field cron parser + `next_after`. |
| `triggers.py` | `Trigger` protocol + `Cron`/`Interval`/`Once`/`Manual` triggers. |
| `backoff.py` | Pure retry policy (exponential + jitter ceiling), reused by store + dispatcher. |
| `registry.py` | `@job(...)` decorator + `JobRegistry`: typed handler lookup, idempotency-key fn. |
| `store.py` | `JobStore` protocol; `InMemoryJobStore`; (`RedisJobStore` lives in `redis_store.py`). |
| `redis_store.py` | Durable Redis-backed run store (atomic claim, DLQ list). |
| `models` (DB) | `scheduled_jobs` + `job_runs` ORM (additive) for the Postgres-durable store. |
| `db_store.py` | `PostgresJobStore` over the new tables (durable, queryable history + DLQ). |
| `lease.py` | `LeaderLease` over Redis `SET NX PX` w/ fencing token + renewal (leader election). |
| `dispatcher.py` | Idempotent at-least-once execution: claim → run handler → ack/retry/DLQ. |
| `scheduler.py` | The `JobScheduler` loop: evaluate triggers under the lease, enqueue due runs. |
| `runner.py` | `JobWorker`: drains due/queued runs from the store and dispatches them. |
| `maintenance.py` | Built-in maintenance job *registrations* (digest/index/GC/recovery/budget). |
| `service.py` | `JobService` facade wiring registry+store+lease+scheduler+worker together. |
| `harness.py` | `VirtualClockHarness`: drive scheduler+worker deterministically in tests. |
| `metrics.py` | `kinora_jobs_*` Prometheus series on the shared registry (runs/retries/DLQ/leader). |
| `__main__.py` | `python -m app.jobs` entrypoint (opt-in process; maintenance jobs no-op-safe). |
| `__init__.py` | Public surface. |

## Semantics / guarantees

- **At-least-once + idempotent.** Each run has a stable `idempotency_key`
  (registry-derived, default `name@scheduled_for-truncated-to-minute`). The store
  refuses to create a second *active* run for the same key, so a double-fire (two
  leaders briefly, a re-run tick, two scheduler nodes) collapses to one logical
  execution. Handlers are expected to be idempotent; the framework guarantees no
  *duplicate enqueue*. **Nuance:** the default key truncates to the minute, so a
  sub-minute interval job would dedup multiple fires within one minute — such jobs
  should pass a finer `idempotency_key=` fn. Cadences here are ≥ 60s, so the
  default is correct for them.
- **Leader election.** Periodic-trigger evaluation runs only on the lease holder
  (Redis `SET NX PX`, owner-token, fencing counter, background renewal). A lost
  lease stops scheduling within one renewal interval. Workers do **not** need the
  lease — any node may drain queued runs (the store's claim is atomic).
- **Retries + backoff + DLQ.** A failed run backs off (exp + jitter ceiling) up to
  a per-job cap, then dead-letters with the captured error. Mirrors §12.1.
- **Triggers.** cron (5-field), fixed interval, one-shot at/after, manual. The
  next fire time is computed against the injected `Clock` so tests are exact.
- **No-op-safe maintenance.** Built-in maintenance jobs are *registrations* that
  resolve their target subsystem lazily and **skip cleanly** (returning a
  `skipped` result) when it isn't wired — so importing/registering them never
  forces the digest/search/GC subsystems to exist.

## Additive shared-file changes (documented per the rules)

- `app/db/models/__init__.py` — **DONE (additive only):** import + export the new
  ORM rows `ScheduledJob` and `JobRun` (the only edit; no existing line changed).
- New Alembic migration `migrations/versions/c0ffeejobs01_jobs_framework_tables.py`
  on the current head `a1b2c3d4e5f6`, unique revision id `c0ffeejobs01`, creating
  `scheduled_jobs` + `job_runs` (+ indexes + a Postgres partial-unique index on
  active `idempotency_key`). Down-rev drops them. No edits to existing migrations.
  Verified: full chain `upgrade head` → `downgrade -1` → `upgrade head` on a clean
  DB.
- `app/core/config.py`, `app/composition.py`, `app/main.py` — **untouched.** The
  framework is self-contained and exercised through `JobService` / the harness, so
  it never needed to enter the composition root (avoids colliding with the nine
  other agents). The opt-in `python -m app.jobs` entrypoint is how a deployment
  would run it; wiring it into the API lifespan is a documented future step, not
  done here.

## Naming note

`JobRun` exists twice by design: the framework **value type**
(`app.jobs.types.JobRun`, used everywhere in `app/jobs`) and the **ORM row**
(`app.db.models.job.JobRun`, exported from `app.db.models`). `db_store.py` imports
the row as `JobRunRow` to keep them distinct.

## Test infra

Isolated: Postgres `kinora_jobs_test` on :5433, Redis db 15
(`KINORA_TEST_REDIS_URL=redis://localhost:6379/15`). All infra-bound tests skip
cleanly when the env vars are unset (mirrors the repo convention). The virtual
clock harness + in-memory store make the *core* logic testable with zero infra.

## Roadmap / milestones

1. **M1 — Core primitives ✅** clock, types, cron, triggers, backoff, registry.
2. **M2 — Stores ✅** in-memory + Redis + Postgres durable stores; DB models + migration.
3. **M3 — Distribution ✅** leader lease + dispatcher (at-least-once, idempotent).
4. **M4 — Loops ✅** scheduler loop + worker + JobService facade + `python -m app.jobs`.
5. **M5 — Maintenance jobs ✅** digest/index/GC/recovery/budget registrations (no-op-safe).
6. **M6 — Test harness ✅** deterministic virtual-clock harness + exhaustive tests.
7. **M7 — Observability ✅** `kinora_jobs_*` Prometheus metrics + structured logging.

### Test coverage (all green; infra-gated tests skip cleanly when unset)
- Unit (no infra): clock, cron, triggers, backoff, registry, in-memory store,
  dispatcher, harness, maintenance, service, metrics, concurrency/failover.
- Infra (Redis db 15): leader lease + elector; Redis durable store contract.
- Infra (Postgres `kinora_jobs_test` :5433): Postgres durable store contract
  (partial-unique-index dedup + `FOR UPDATE SKIP LOCKED` claim).

## Remaining / future roadmap

- **API admin surface** (`app/api`): list jobs / runs, force-run, pause/resume,
  drain + replay DLQ — `JobService` already exposes every operation it needs.
- **Composition wiring**: start a `JobService` in the API lifespan (or a dedicated
  `jobs-worker` compose service running `python -m app.jobs`) behind a settings
  flag; inject the real maintenance resources (digest/search/GC/recovery/budget).
- **Durable scheduler bookmark**: persist per-job `last_fire` to `scheduled_jobs`
  so a leader restart resumes exactly (today it re-derives; idempotency covers it).
- **Richer cron**: seconds field, `@hourly`/`@daily` macros, timezone-aware
  evaluation.
- **Calendar/backfill**: catch-up policy controls (fire-missed vs. skip-to-now).
