# DESIGN.md — Redis priority render queue + worker (domain owner)

Living roadmap for the distributed render-job system under `backend/app/queue/`.
Owner agent domain: `backend/app/queue/` (redis_queue.py, enqueuer.py, worker.py)
plus new owned modules. kinora.md §12.1–§12.3, §4.8, §4.9.

## Owned files
- `backend/app/queue/redis_queue.py` — three-lane priority queue (pre-existing).
- `backend/app/queue/enqueuer.py` — memory-layer render seam (pre-existing).
- `backend/app/queue/worker.py` — async consumer + dedicated lane pools (pre-existing).
- `backend/app/queue/fakeredis.py` — **NEW** dependency-free async Redis double.
- `backend/app/queue/backoff.py` — **NEW** exponential backoff with jitter.
- `backend/app/queue/admission.py` — **NEW** backpressure + per-session fairness.
- `backend/app/queue/dlq.py` — **NEW** DLQ inspect / replay / purge tooling.
- `backend/app/queue/leases.py` — **NEW** lease / visibility-timeout helpers.
- `backend/app/queue/autoscale.py` — **NEW** depth-driven worker-pool autoscaler.
- `backend/tests/test_queue_fakeredis.py` — harness self-tests.
- `backend/tests/test_queue_unit.py` — full queue behaviour via the fake (no infra).
- `backend/tests/test_queue_worker_unit.py` — worker behaviour via the fake.
- `backend/tests/test_queue_backoff.py`, `_admission.py`, `_dlq.py`,
  `_leases.py`, `_autoscale.py` — per-module unit tests.

## Additive shared-file changes
- `backend/app/core/config.py` — *additive only*: new optional queue-tuning settings
  (jitter, per-session cap, autoscale bounds). Defaults preserve current behaviour.

## Phases
1. **fakeredis harness** ✅ — in-process async Redis double covering exactly the
   queue+worker surface (strings/hashes/sets/zsets/lists/eval/scan/TTL/pubsub),
   with a Lua-fingerprint guard. Makes the whole system unit-testable with no infra.
2. **Queue + worker unit tests via the fake** ✅ — port the 18 infra-gated behaviours
   to run everywhere; add edge cases (lease expiry/renewal, reaper, preemption,
   cancel-distant ETA math, dedup across sessions).
3. **Backoff with jitter** (`backoff.py`) ✅ — decorrelated/full jitter schedules;
   the existing fixed `RetryPolicy` keeps working, jitter is opt-in.
4. **Admission control** (`admission.py`) ✅ — depth backpressure + per-session
   max-concurrent fairness (§12.2 "per-session fairness").
5. **DLQ tooling** (`dlq.py`) ✅ — inspect, peek, replay (re-enqueue), purge,
   age stats — the operability layer §12.1 implies but didn't ship.
6. **Lease manager** (`leases.py`) ✅ — visibility-timeout + renewal abstraction
   decoupled from the worker, plus a standalone reaper helper.
7. **Autoscaler** (`autoscale.py`) ✅ — compute desired pool sizes per lane from
   live depth + inflight, with min/max clamps and cooldown (anti-flap).
8. **Wiring + config** ✅ — additive settings; queue grows opt-in jitter + the
   admission/lease/dlq hooks without changing default behaviour.

## Status (this round — all green)
- `make lint` clean (ruff + mypy over `app tests`, 385 source files).
- Full backend suite: **1151 passed** (was 1041 baseline), 160 skipped, 0 failed.
- 110 new tests, all infra-free (run anywhere): harness 13, queue-unit 19,
  worker-unit 16, backoff 11, admission 17, dlq 13, leases 7, autoscale 11, plus
  the jitter-wiring + config-wiring cases.
- The 18 previously infra-gated `test_queue_redis.py` / `test_queue_worker.py`
  behaviours are now *also* covered with no infra (the originals stay, still
  exercising real Redis when `KINORA_TEST_REDIS_URL` is set).

## Additive shared-file changes (delivered)
- `backend/app/core/config.py` — new optional settings only, defaults preserve
  current behaviour: `queue_backoff_jitter` (none|full|equal|decorrelated),
  `queue_backoff_base_s`/`_cap_s`, `queue_retry_backoff_s`,
  `queue_backpressure_depth`, `queue_session_render_cap`,
  `queue_autoscale_{committed,speculative}_max`, `queue_autoscale_cooldown_s`.
- `backend/app/queue/redis_queue.py` (owned) — `RedisRenderQueue(..., backoff=…)`
  optional param; when set, materialises a seeded jittered schedule into RetryPolicy.
- `backend/app/queue/worker.py` (owned) — `build_worker` reads the new settings,
  passes a `BackoffSchedule` + backpressure depth into the queue.
- `backend/app/queue/__init__.py` (owned) — exports the new public types.

## Remaining roadmap (next rounds)
- Wire `SessionFairness.acquire/release` into the worker's claim/ack lifecycle so
  the per-session in-flight tally is maintained automatically (currently the
  controller can read it; the worker doesn't yet write it). Gate on
  `queue_session_render_cap > 0`.
- Wire `LaneAutoscaler` into a control loop (api process or a dedicated supervisor)
  that pushes desired counts to the worker's TaskGroup or an ECS desired-count;
  emit an `autoscale_desired` gauge per lane (§12.5).
- Surface a DLQ admin route (inspect/replay/purge) behind the MCP/admin auth, and
  a `dlq_age_seconds` metric for the §12.5 panel + an alert.
- Optional: a `cancel_distant` pre-check in `AdmissionController` so a seek can
  shed admission before the round-trip.

## Invariants preserved
- `KINORA_LIVE_VIDEO` OFF; zero credits. Nothing here calls a provider.
- Redis remains the authoritative queue; the Postgres mirror is best-effort.
- Idempotency key = `shot_hash`; committed always admitted; speculative droppable.
- No edits to other agents' domains; shared files touched additively only.
