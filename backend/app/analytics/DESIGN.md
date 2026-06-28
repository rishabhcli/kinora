# Product Analytics & Event Pipeline — `app/analytics/`

A product-analytics subsystem **distinct** from two neighbours:

* **ops-observability** (`app/observability/`) — Prometheus counters/gauges for
  infra/SRE (HTTP rate, render latency, buffer occupancy). That answers *"is the
  service healthy right now?"*.
* **the §13 quality / eval warehouse** (`app/eval/`) — CCS, accepted-footage
  efficiency, regen rate, the A/B harness. That answers *"is the crew beating the
  single-agent baseline on consistency & budget?"*.

This subsystem answers the **third, orthogonal** question: *"how do humans use the
product?"* — sessionized reading behaviour, funnels, retention, cohorts, and
reading-engagement metrics (pages/min, completion, drop-off). It is a typed
**event pipeline** with batched/idempotent ingestion, rollups into summary
tables, and a flexible time-bucketed query API. PII never lands in storage.

## Why a separate pipeline (not Prometheus, not the eval warehouse)

* Product analytics needs **per-event, per-user, retained, queryable** rows —
  high-cardinality, long-lived, slice-and-dice. Prometheus is the opposite
  (aggregate, bounded cardinality, short retention).
* It needs **sessionization** (gap-based event stitching) and **cohort/retention**
  math the eval harness has no notion of.
* It must be **PII-safe at the boundary**: emails, raw note text, file names, etc.
  are scrubbed/hashed *before* persistence (`scrub.py`).

## Module map

| Module | Responsibility |
|---|---|
| `events.py` | The typed event taxonomy (`EventName` enum), `EventEnvelope`/`TrackedEvent` pydantic models, validation, canonicalisation. The contract every producer speaks. |
| `scrub.py` | PII-safe scrubbing: deterministic hashing of identifiers, email/URL/path redaction, free-text length capping, property allow/deny lists. Pure functions. |
| `store.py` | The `AnalyticsStore` protocol + a deterministic **in-memory** implementation (`InMemoryAnalyticsStore`) for tests. Idempotent batched append keyed on `event_id`. |
| `sessionize.py` | Gap-based sessionization: turn an ordered event stream into reading sessions (30-min inactivity gap by default), compute per-session engagement (duration, pages, words, pages/min, completion, drop-off page). |
| `funnel.py` | Ordered-step funnel analysis with a conversion window; per-step counts, conversion %, drop-off, median time-to-convert. |
| `retention.py` | N-day / N-week retention matrices, classic cohort retention (triangle), rolling/unbounded retention. |
| `cohorts.py` | Cohort assignment (signup-period, acquisition-source, first-event) + cohort metric rollups. |
| `engagement.py` | Reading-engagement metrics: pages/min, words/min, completion ratio, drop-off page, viewer-vs-director split, dwell histograms. |
| `rollup.py` | Aggregation jobs that fold raw events → daily/period summary rows (DAU/WAU/MAU, event-type counts, engagement aggregates) in summary tables. Idempotent upsert keyed on `(bucket, dimension, metric)`. |
| `query.py` | The flexible query layer: time-bucketing (`hour`/`day`/`week`/`month`), metric selection, group-by dimensions, filters, top-N. Runs over a store. |
| `timebucket.py` | Pure time-bucketing helpers (floor-to-bucket, bucket ranges, ISO labels) shared by rollup + query. |
| `service.py` | `AnalyticsService` — the façade the API route + rollup job call. Ingest (scrub→validate→store), query, sessionize, funnel, retention, `run_rollup_job`. |
| `sink.py` | The `SummarySink` seam + in-memory impl: idempotent persistence of rollup rows + derived sessions. |
| `sink_pg.py` | Postgres-backed `SummarySink` over the repo. |
| `store_pg.py` | Postgres-backed `AnalyticsStore` over the repo. |
| `rollup_worker.py` | Long-running worker (`python -m app.analytics.rollup_worker`) — re-aggregates the trailing window into the summary tables on a cadence (mirrors the ingest-recovery worker process model). |
| `db/models/analytics.py` | `analytics_events`, `analytics_sessions`, `analytics_daily_rollup` ORM tables. |
| `db/repositories/analytics.py` | Postgres repo backing the Postgres store. |
| `store_pg.py` | Postgres-backed `AnalyticsStore` over the repo. |

## DB tables (Alembic `f3a91c7d20e4`, additive, on head `a1b2c3d4e5f6`)

* `analytics_events` — one row per scrubbed event. `event_id` UNIQUE (idempotency).
  Indexed on `(book_id, occurred_at)`, `(anon_user_id, occurred_at)`,
  `(name, occurred_at)`, `session_key`.
* `analytics_sessions` — one row per derived reading session (rollup output of
  sessionize). UNIQUE `session_key`.
* `analytics_daily_rollup` — `(bucket_start, granularity, dimension_key, metric)`
  summary grain. UNIQUE for idempotent upsert.

## Shared-file changes (additive only, documented here)

* `core/config.py` — append analytics settings (`analytics_enabled`,
  `analytics_session_gap_s`, `analytics_max_batch`, `analytics_retention_days`,
  `analytics_salt`). Additive.
* `db/models/__init__.py` — import + export the three analytics models. Additive.
* `composition.py` — lazy `analytics_service()` builder + store seam. Additive.
* `api/routes/__init__.py` — append `analytics.router`. Additive.
* New Alembic migration on the current head with a UNIQUE revision id.

## Milestones / roadmap

1. **M1 — Event taxonomy + scrubbing + in-memory store** (pure, no infra).
2. **M2 — Sessionization + engagement metrics.**
3. **M3 — Funnel + retention + cohort analysis.**
4. **M4 — Time-bucketing + flexible query layer.**
5. **M5 — DB tables + repo + Postgres store + Alembic migration.**
6. **M6 — Rollup/aggregation jobs into summary tables.**
7. **M7 — Ingestion endpoint (batched, idempotent) + query API + service façade.**
8. **M8 — Wiring (config, composition, route registration) + full test pass.**

Remaining / future: scheduled rollup worker command (mirroring ingest-worker),
streaming export to the eval warehouse, p50/p95 latency percentile sketches,
materialised funnel cache. See `## Status` at the bottom for the live state.

## Test strategy

* Pure units (`events`, `scrub`, `sessionize`, `funnel`, `retention`, `cohorts`,
  `engagement`, `timebucket`, `query`, `rollup` over the in-memory store) run with
  **no infra** and are the bulk of coverage.
* DB-backed store + repo + the ingestion API route run only when
  `KINORA_TEST_DATABASE_URL` (+ redis/s3 for the gateway) is set; they **skip
  cleanly** otherwise (isolated DB `kinora_analytics_test` on :5433).

## Status

- [x] **M1** — event taxonomy (`events.py`) + PII scrubbing (`scrub.py`) +
  in-memory store (`store.py`). Pure, no infra.
- [x] **M2** — sessionization (`sessionize.py`) + engagement (`engagement.py`).
- [x] **M3** — funnel (`funnel.py`) + retention (`retention.py`) + cohorts
  (`cohorts.py`).
- [x] **M4** — time-bucketing (`timebucket.py`) + flexible query (`query.py`).
- [x] **M5** — DB tables (`db/models/analytics.py`) + repo
  (`db/repositories/analytics.py`) + Postgres store (`store_pg.py`) + Alembic
  migration `f3a91c7d20e4` (on head `a1b2c3d4e5f6`). Migration upgrade/downgrade
  verified on a fresh DB; zero autogenerate drift for the analytics tables.
- [x] **M6** — rollup/aggregation (`rollup.py`) + idempotent upsert in the repo.
- [x] **M7** — ingestion + query API (`api/routes/analytics.py`) + service façade
  (`service.py`).
- [x] **M8** — wiring (config settings, `Container.analytics_service()`, route
  registration). `make lint` + `make test` green (108 new pure unit tests; 20
  infra-bound tests pass against the isolated `kinora_analytics_test` DB + redis
  db 15 + MinIO, skip cleanly otherwise).

### Test coverage

| Suite | File | Infra |
|---|---|---|
| event taxonomy | `tests/test_analytics_events.py` | none |
| PII scrubbing | `tests/test_analytics_scrub.py` | none |
| time-bucketing | `tests/test_analytics_timebucket.py` | none |
| in-memory store | `tests/test_analytics_store.py` | none |
| sessionization | `tests/test_analytics_sessionize.py` | none |
| engagement | `tests/test_analytics_engagement.py` | none |
| funnel | `tests/test_analytics_funnel.py` | none |
| retention | `tests/test_analytics_retention.py` | none |
| cohorts | `tests/test_analytics_cohorts.py` | none |
| query | `tests/test_analytics_query.py` | none |
| rollup | `tests/test_analytics_rollup.py` | none |
| service façade | `tests/test_analytics_service.py` | none |
| repo + PG store | `tests/test_analytics_repo_db.py` | Postgres |
| HTTP API | `tests/test_api_analytics.py` | full gateway |

### Remaining / future roadmap

- A scheduled rollup-worker command (`python -m app.analytics.rollup_worker`)
  mirroring the ingest-worker, to persist `compute_rollups` output on a cadence.
- A retention/prune job honouring `analytics_retention_days`.
- Persisting derived sessions (`upsert_sessions`) on a cadence (repo method
  exists; no scheduler yet).
- Percentile sketches (p50/p95) for latency-style props; streaming export of
  product metrics into the §13 eval warehouse for a unified dashboard.
- Client-side event emission from `apps/desktop` (the renderer is out of this
  backend domain).
