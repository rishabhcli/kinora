# DESIGN.md — Providers Resilience Gateway

> Living roadmap for the **provider resilience gateway** domain. All work here is
> additive to round-1's provider layer and never edits round-1 files (`audio.py`,
> `prosody.py`, `video_router.py`, `video.py`, `tts.py`, `chat.py`, `image.py`,
> `vl.py`, `embeddings.py`, `errors.py`, `types.py`).

## Scope & ownership

Owned files (free to edit/create):
- `backend/app/providers/base.py` *(round-2 owner; additive-only changes that keep
  the round-1 `ProviderClient` API intact)*
- `backend/app/providers/__init__.py` *(additive exports + opt-in wiring)*
- **New gateway/resilience modules** under `backend/app/providers/resilience/`.

Additive shared-file changes (documented at bottom): `core/config.py`. Append-only.

## Goal (task brief + kinora.md §11.1 / §12.1 / §12.3)

A hardened provider gateway around the shared `ProviderClient`:
1. **Per-model circuit breakers** with half-open probing.
2. **Adaptive token-bucket rate limiting** that backs off on 429s (AIMD).
3. **Retries with full jitter** (full / equal / decorrelated jitter schedules).
4. **Hedged / duplicate requests** for tail-latency cuts.
5. **Response cache** keyed by a stable request hash (+ in-flight dedup, §12.3).
6. **Multi-cloud provider-abstraction registry** (DashScope/OpenAI/others) with
   capability negotiation.
7. **Unified usage metering** into the budget sink (one `Usage` currency, §11.1).
8. **Fault-injection + chaos test suite.**

Hard rule overriding everything: `KINORA_LIVE_VIDEO` OFF, zero credits.
`LiveVideoDisabled` is a deliberate spend gate, never counted as a fault.

## Module map (`backend/app/providers/resilience/`)

| Module | Responsibility | Status |
|---|---|---|
| `__init__.py` | package aggregator + public re-exports | done |
| `backoff.py` | jitter schedules (full / equal / decorrelated) + Retry-After | done |
| `ratelimit.py` | `AdaptiveTokenBucket` — AIMD rate that drops on 429 | done |
| `breakers.py` | `BreakerRegistry` — per-model breakers, half-open probing | done |
| `cache.py` | `ResponseCache` — request-hash keyed TTL+LRU + in-flight dedup | done |
| `hedging.py` | `HedgedExecutor` — duplicate request tail-cut | done |
| `metering.py` | `MeteringSink` — fan-out + per-model usage rollups | done |
| `registry.py` | `ProviderRegistry` + `Capability` negotiation | done |
| `stats.py` | snapshot/inspection dataclasses for tests + telemetry | done |
| `gateway.py` | `ResilientGateway` — composes all of the above | done |
| `factory.py` | build gateway + registry from `Settings` (config translation) | done |
| `facade.py` | `GatewayChatProvider`/`GatewayCallable` — wrap round-1 providers | done |
| `degradation.py` | `DegradationAdvisor` — §11.1 budget-floor → degrade level | done |

## Phases

- **P1 — backoff schedules** (`backoff.py`). DONE
- **P2 — adaptive rate limiter** (`ratelimit.py`). DONE
- **P3 — per-model breaker registry** (`breakers.py`). DONE
- **P4 — response cache + in-flight dedup** (`cache.py`). DONE
- **P5 — hedged execution** (`hedging.py`). DONE
- **P6 — unified metering sink** (`metering.py`). DONE
- **P7 — multi-cloud registry + capability negotiation** (`registry.py`). DONE
- **P8 — gateway composition + stats** (`gateway.py`, `stats.py`). DONE
- **P9 — chaos / fault-injection harness + suite** (`chaos.py` + tests). DONE
- **P10 — config wiring (additive, opt-in)** + `factory.py`. DONE
- **P11 — depth: gateway-wrapped provider facades** (`facade.py`) — compose the
  round-1 `ChatProvider`/any async method through the gateway without editing it. DONE
- **P12 — depth: §11.1 budget-floor degradation advisor** (`degradation.py`) —
  maps metered video-seconds against a budget window to a degrade level + the
  `budget_low` UI signal, read-only (never touches the spend gate). DONE

## Key bug found + fixed during the chaos suite

`AdaptiveTokenBucket.acquire` could spin forever when a refill landed a hair below
the requested token count (float dust): the sub-epsilon `wait_s`, added to a large
clock value, rounded away to zero elapsed time. Fixed with a `_TOKEN_EPSILON`
tolerance on the availability check (and `make_async_sleep` now yields to the loop).

## Additive shared-file changes

- `core/config.py`: appended a `# --- Provider resilience gateway ---` block of
  opt-in settings (all defaulted; gateway opt-in via `provider_gateway_enabled`).

## Test results

`backend/tests/test_providers_resilience_*.py` — all green under
`backend/.venv/bin/pytest backend/tests/ -q` with no infra.
---

# DESIGN.md — Budget & Cost Governance (FinOps)

Living roadmap for the FinOps domain. Owner: the FinOps agent.
Cites `kinora.md` §11.1 (the budget reality + accounting system) and §4.4 (zones —
speculation is image-only, video-seconds are the scarce currency).

## Mission

Video-seconds are the hard-capped, scarce currency (~1,650s over the project's
lifetime, §11.1). The existing `BudgetService` (reserve/commit/release over an
append-only `budget_ledger`, advisory-lock-serialized) is the **load-bearing
guardrail** and MUST keep its contract — the Scheduler (`§4.6` promotion) and the
render pipeline (`§9.7`) both call it and must not change.

This domain builds a full **FinOps layer** *on top of* that contract:

1. **Multi-scope budgets** — tenant + per-session + per-scene + global, all as
   windowed sums over the same ledger, advisory-lock-serialized so two
   reservations cannot both slip past a cap.
2. **Cost forecasting** — project future video-seconds from a reading trajectory
   (velocity, remaining words, promotion rate) so the UI/Scheduler can see a
   burn-down and an ETA-to-exhaustion before it happens.
3. **Cost attribution** — per-agent and per-shot USD/physical attribution built
   on the existing `optim.cost_meter` `Usage` stream + the video ledger.
4. **Quality↔budget optimizer** — pick the render mode (full video / animatic /
   keyframe+KenBurns / text) that maximizes total quality under the remaining cap,
   given each mode's cost and expected quality.
5. **Tiered caps + alerts** — soft cap (warn), hard cap (the existing `BudgetExceeded`),
   floor (degrade), with per-scope tiered alert levels.
6. **Auditable cost ledger + reconciliation** — an append-only USD cost ledger
   (distinct from the physical video-seconds ledger) that can be reconciled against
   the video ledger and the cost meter, surfacing drift.
7. **Simulation harness** — a no-infra harness that drives synthetic reading
   sessions through forecasting + the optimizer to prove the system stays inside
   budget (KINORA_LIVE_VIDEO OFF, zero credits).

## Hard rules

- `KINORA_LIVE_VIDEO` stays OFF; zero credits. Everything here is accounting/sim.
- Never weaken the existing reserve/commit/release contract; only extend.
- Additive-only on shared files: `core/config.py` (new settings), `db/models/__init__.py`
  (new table exports), `composition.py` (new wiring). Documented below.
- New DB table => an Alembic migration chaining on the current head `a1b2c3d4e5f6`.

## Package layout — `backend/app/finops/`

- `tiers.py`          — budget scopes, tier thresholds, alert levels, `TieredCap` policy.
- `forecast.py`       — reading-trajectory cost forecasting + burn-down + exhaustion ETA.
- `attribution.py`    — per-agent / per-shot cost attribution over Usage + the video ledger.
- `optimizer.py`      — quality↔budget render-mode optimizer (greedy + knapsack-ish).
- `ledger.py`         — append-only USD cost ledger model wrapper + reconciliation.
- `governor.py`       — the orchestration facade: ties scopes+tiers+forecast+optimizer
                        into one budget-governance service the API/scheduler can read.
- `simulation.py`     — no-infra synthetic-session harness (proves we stay in budget).
- `service.py`        — `FinOpsService` aggregate exposed via the container.

DB: `backend/app/db/models/finops.py` — `cost_ledger`.
Repo: `backend/app/db/repositories/finops.py`.
Migration: `backend/migrations/versions/<rev>_finops_cost_ledger.py` (down=a1b2c3d4e5f6).

## Additive shared-file changes (documented)

- `core/config.py`: `finops_*` settings (tenant ceiling, soft-cap fraction, alert
  fractions, forecast horizon, optimizer mode prices/quality). Additive fields only.
- `db/models/__init__.py`: export `CostLedger`, `CostKind`. Additive.
- `composition.py`: build a `FinOpsService` lazily; expose `container.finops`. Additive.
- New API route `routes/finops.py` (own file) mounted in `routes/__init__.py` (additive).

## Milestones

- [x] M0 — Read §11.1/§4.4, study budget service/repo/scheduler/pipeline; green baseline.
- [x] M1 — `finops/tiers.py`: scopes + tiered caps + alert levels (pure, unit-tested).
- [x] M2 — `finops/forecast.py`: trajectory cost forecast + burn-down + exhaustion ETA.
- [x] M3 — `finops/attribution.py`: per-agent / per-shot attribution over Usage.
- [x] M4 — `finops/optimizer.py`: quality↔budget render-mode optimizer.
- [x] M5 — `finops/ledger.py` + DB `cost_ledger` model + repo + migration + reconciliation.
- [x] M6 — `finops/governor.py` + `service.py`: the governance facade; advisory-locked
           multi-scope reservation wrapping the existing BudgetService.
- [x] M7 — `finops/simulation.py`: no-infra synthetic-session harness.
- [x] M8 — API route `routes/finops.py` + composition wiring + config + tests green.
- [x] M9 — depth pass: EWMA `VelocityEstimator` (smooth noisy velocity for forecasts);
           `simulate_pool` (many tenants, ONE shared global ceiling — proves
           "no one drains the pool" under contention).

## Status / notes — DELIVERED

- New migration `c1d2e3f4a5b6` chains on head `a1b2c3d4e5f6`; verified upgrade+downgrade
  on a fresh DB and `alembic check` reports no `cost_ledger` drift (the 3 pre-existing
  vector/covering-index drifts are unrelated to FinOps).
- Tests: 72 new pure unit tests (tiers/forecast/optimizer/attribution/governor/ledger/
  sim + API-simulate) all green with no infra; 21 DB-gated tests (FinOpsService +
  gateway routes + the unchanged budget contract) pass on an isolated DB
  (`kinora_finops_test`) + redis db 15. Full suite: 1112 passed, 0 failed.
- Additive shared-file changes (all documented above): `core/config.py` (`finops_*`),
  `db/models/__init__.py` (`CostKind`/`CostLedger`), `db/repositories/__init__.py`
  (`CostLedgerRepo`), `db/repositories/budget.py` (added `used_seconds_for_books` —
  this file is in the FinOps domain), `composition.py` (`finops_policy` field +
  `build_finops`), `api/routes/__init__.py` (mount `finops.router`).
- Reserve/commit/release contract PRESERVED: `FinOpsService` wraps `BudgetService`
  unchanged; the tenant cap is an *additional* check under the same advisory lock.
- Pre-existing flaky test on main (NOT this domain): `test_scheduler_experiment.py::
  test_ab_deeper_buffer_is_at_least_as_smooth` — order/state-dependent; passes in the
  full-suite run. Left untouched (scheduler domain).
---

# DESIGN.md — Observability & Telemetry domain

Owner: telemetry agent (isolated worktree). This file is the living roadmap for
the **observability & telemetry** workstream. It is additive on shared files and
authoritative for the files this agent owns.

## Owned files

* `backend/app/telemetry/` — **NEW package** (this agent's primary domain).
* `backend/app/api/routes/metrics.py` — extended with a warehouse / SLO read
  surface (additive endpoints; the existing eval endpoints are untouched).
* `backend/tests/test_telemetry_*.py` — the test suite for the package.

## Relationship to the existing `app/observability/` package

`app/observability/` already exists and is owned by a *different* concern. It
provides the low-level Prometheus `CollectorRegistry`, the typed emit helpers
(`observe_render_latency`, `inc_cache`, `set_buffer_occupancy`, …), the
`/metrics` exposition, and an env-gated OpenTelemetry **FastAPI** instrumentation
(`init_tracing`).

`app/telemetry/` is the **higher layer** built *on top of* that. It does not
duplicate the Prometheus series; it imports the emit helpers and adds:

1. **Correlation / trace context** — contextvars + a structlog processor so every
   log line carries `correlation_id` / `trace_id` / `span_id` without call sites
   passing them around.
2. **A dependency-free span/tracer** — a pure-Python tracer that records spans,
   propagates context across the six-agent crew, and transparently *bridges* to
   real OpenTelemetry when the SDK is installed and configured. The default
   exporter is a no-op, so **nothing requires a collector to run the tests**.
3. **RED** (Rate/Errors/Duration) helpers for the API and **USE**
   (Utilization/Saturation/Errors) helpers for the workers + queue.
4. **Per-agent quality/cost warehouse (§13)** — an in-process, thread-safe
   aggregator of per-agent calls, tokens, latency, repairs, QA scores, render
   outcomes and video-seconds; snapshot-able to a report dict and re-exported as
   Prometheus gauges.
5. **SLOs + multi-window burn-rate** alerting math, plus **dashboards-as-code**
   (Grafana JSON) and **Prometheus alerting rules** generated from the SLO set.

The dividing rule: `observability/` knows about Prometheus types; `telemetry/`
knows about *meaning* (what an agent is, what an SLO is, what a burn rate is) and
stays Prometheus-type-free at its call sites.

## Additive shared-file changes (documented per the rules)

* `backend/app/api/routes/metrics.py` — **owned** by this agent; new endpoints
  `GET /api/eval/warehouse`, `GET /api/eval/slo`, `GET /api/eval/slo/alerts`,
  `GET /api/eval/dashboards/{name}` added; existing eval endpoints untouched.
* No edits to `core/config.py`, `main.py`, or `composition.py` are required: the
  package is import-safe and self-initialising. (If later wiring is added it will
  be strictly additive and listed here.)

## Modules & milestones

| Module | Purpose | Status |
|---|---|---|
| `telemetry/context.py` | correlation/trace/span contextvars + structlog processor | done |
| `telemetry/exporters.py` | no-op + in-memory span exporters | done |
| `telemetry/spans.py` | dependency-free tracer + OTel bridge + W3C propagation | done |
| `telemetry/crew.py` | per-agent spans threaded across the six-agent crew | done |
| `telemetry/red.py` | RED (rate/errors/duration) helpers for the API | done |
| `telemetry/use.py` | USE (utilization/saturation/errors) for workers/queue | done |
| `telemetry/warehouse.py` | §13 per-agent quality/cost aggregation warehouse | done |
| `telemetry/domain.py` | typed domain-metric facade (buffer/render/QA/budget) | done |
| `telemetry/slo.py` | SLO objects + multi-window burn-rate math | done |
| `telemetry/alerts.py` | Prometheus alerting rules from the SLO set | done |
| `telemetry/dashboards.py` | Grafana dashboards-as-code (JSON model) | done |
| `telemetry/promstore.py` | per-agent warehouse → Prometheus gauge mirror | done |
| `telemetry/middleware.py` | drop-in correlation/RED ASGI middleware + log splice | done |
| `telemetry/__init__.py` | public facade | done |
| `api/routes/metrics.py` | warehouse / SLO / alerts / dashboard read endpoints | done |

## Endpoints added to `api/routes/metrics.py` (owned file)

* `GET /api/eval/warehouse` — live per-agent quality/cost rollup (also mirrors to
  Prometheus on read).
* `GET /api/eval/slo` — the SLO catalogue (objectives, SLI queries, burn windows).
* `GET /api/eval/slo/alerts?fmt=json|yaml` — Prometheus recording + multi-window
  burn-rate alerting rules derived from the SLO set.
* `GET /api/eval/dashboards` / `GET /api/eval/dashboards/{name}` — Grafana
  dashboards-as-code (`overview` = RED+USE+budget; `crew` = §13 per-agent panel).

All require an authenticated user; none touch the DB/Redis/a provider.

## Invariants

* **Zero credits, KINORA_LIVE_VIDEO off.** Telemetry never calls a model.
* **No-collector default.** With no OTel SDK and no OTLP endpoint, every span op
  is a cheap no-op recording into an in-process ring buffer; tests pass offline.
* **Bounded cardinality.** Per-agent series are keyed by the six fixed crew roles;
  no unbounded label (no per-session/per-shot Prometheus label is minted here).
* **Never break startup.** Every OTel import is lazy + guarded; failures degrade
  to the pure-Python path and are logged, never raised.

## Adoption (one additive line each, no shared-file edit done here)

To turn on request-time correlation + RED + the structlog id splice, add to
`app.main.create_app` (kept out of this worktree to avoid clashing with the nine
parallel agents touching `main.py`):

```python
from app.telemetry.middleware import CorrelationMiddleware, install_correlation_logging
install_correlation_logging()
app.add_middleware(CorrelationMiddleware)
```

The render worker can continue a request's trace by passing the job carrier to
`app.telemetry.use.track_job(lane, carrier=job_headers)`; the enqueuing request
stamps it with `app.telemetry.spans.inject_context()`.

## Verification status

* `make lint` (ruff + mypy over 394 source files): GREEN.
* `make test` (no infra): 1143 passed, 168 skipped, 0 failed. 102 new telemetry
  unit tests + 8 infra-gated API tests (skip offline, pass against isolated infra).
* The 8 telemetry API tests pass end-to-end against the isolated stack
  (`kinora_conflict_test` DB + redis db 15 + minio).

## Roadmap / remaining

* Optional: adopt the middleware + worker carrier wiring above (one line each).
* Optional: persist warehouse snapshots to Redis for cross-process aggregation.
* Note (outside this domain): `tests/conftest.py`'s autouse `_isolate_state`
  truncate can race ahead of the `auth_headers` fixture under real isolated
  infra, intermittently 401-ing *all* API gateway tests (reproduced on the
  pre-existing `test_eval_api.py` too). Flagged as a separate task.
---

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
