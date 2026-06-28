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
