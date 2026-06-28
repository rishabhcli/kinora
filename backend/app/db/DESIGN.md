# DB infrastructure layer — design & roadmap

Owner domain: **Database / data-access infrastructure** under `backend/app/db/`.

This is the *shared* infrastructure the concrete repositories (book, shot,
entity, continuity, budget, bitemporal, …) can adopt. It does **not** touch other
domains' specific models/repos; it only adds reusable building blocks plus
purely-additive registration.

Read first: `kinora.md` §8 (memory layer / canon / episodic / hashing / caching),
§12 (the unglamorous 30%: queue, concurrency, caching layers, observability,
Alibaba deployment). The data layer is the floor those sections stand on.

## Design principles

1. **Lazy & side-effect-free imports.** Importing any module here opens no
   sockets and needs no network (mirrors `composition.py` / `session.py`). The
   engine, pools, and listeners are created on first use.
2. **The unit-of-work boundary owns the transaction.** Repositories *flush*,
   never *commit* (existing `BaseRepository` contract). The new `UnitOfWork`
   formalises that boundary and adds savepoint/nested-transaction support and
   retry-on-serialization-failure.
3. **Read/write split is opt-in and safe.** A read-replica engine is only built
   when a replica URL is configured; otherwise reads transparently fall back to
   the primary. Writes always go to the primary.
4. **Everything is typed.** `disallow_untyped_defs = true`, mypy clean. The
   generic repository is `Generic[ModelT, IdT]`.
5. **Postgres-first but degrade gracefully.** Slow-query / EXPLAIN helpers use
   Postgres features (`EXPLAIN (FORMAT JSON)`, `pg_stat_statements` when present)
   and no-op / raise informative errors elsewhere.

## Module map (all under `app/db/`)

| Module | Responsibility | Status |
|---|---|---|
| `engine.py` | Typed `EngineConfig`, primary + optional read-replica engine builders, pool tuning, `EngineRegistry`, slow-query event listeners, connection health checks. | Phase 1 |
| `routing.py` | Read/write split: `RoutingSessionFactory` choosing primary vs replica by intent. | Phase 1 |
| `health.py` | Connection-pool health checks + pool stats snapshot for `/ready` + observability (§12.5). | Phase 1 |
| `mixins.py` | `SoftDeleteMixin`, `AuditMixin`, `VersionedMixin` (optimistic-concurrency `version_id`) + soft-delete query predicates. | Phase 2 |
| `unit_of_work.py` | `UnitOfWork` async context manager: commit/rollback, `savepoint()`, retry-on-serialization-failure, repository registry. | Phase 3 |
| `repositories/generic.py` | `GenericRepository[ModelT, IdT]`: typed CRUD, pagination, filtering, soft-delete-aware reads, optimistic-version bump, `exists`, `count`. | Phase 3 |
| `retry.py` | `with_db_retry` / classify transient PG errors (40001/40P01 + disconnects), exponential backoff with jitter. | Phase 4 |
| `query.py` | Query helpers: `paginate`, `apply_filters`, `apply_ordering`, keyset (cursor) pagination, `Page` result, batched `IN` chunking. | Phase 5 |
| `inspect.py` | Query-plan / slow-query inspector: `explain()`, `explain_analyze()`, plan-cost extraction, seq-scan detection, `pg_stat_statements` top-N, captured slow-query ring buffer. | Phase 6 |
| `bulk.py` | Bulk-load helpers: chunked `bulk_insert`, `bulk_upsert` (PG `ON CONFLICT`), returning ids. | Phase 7 |
| `migration_safety.py` | Online-migration patterns + backfill helpers: lock-timeout guards, `CREATE INDEX CONCURRENTLY`, expand/contract helpers, batched backfill runner, safety linter for risky DDL. | Phase 8 |

## Roadmap (phase by phase)

- **Phase 1 — engine/routing/health.** ✅ Typed engine config + registry, replica
  routing, pool health snapshot. Foundation everything else binds to.
- **Phase 2 — mixins.** ✅ Soft-delete, audit, optimistic-version column mixins +
  query predicates.
- **Phase 3 — generic repo + UoW.** ✅ The base the concrete repos can adopt.
- **Phase 4 — retry / optimistic concurrency.** ✅ Transient-error classifier +
  retrying UoW; `StaleDataError` surfacing.
- **Phase 5 — query helpers.** ✅ Offset + keyset pagination, filter/order DSL,
  IN-chunking.
- **Phase 6 — query-plan / slow-query inspector.** ✅ EXPLAIN wrappers, seq-scan
  warnings, pg_stat_statements, captured slow-query ring buffer.
- **Phase 7 — bulk-load.** ✅ Chunked insert/upsert with conflict policies.
- **Phase 8 — migration-safety toolkit.** ✅ Online DDL patterns + batched backfill.

## Additive shared-file changes (documented per the worktree rules)

- `app/db/__init__.py` — extended the package docstring to mention the new infra
  modules (doc-only).
- `core/config.py` — **additive only**: optional `database_replica_url` plus pool
  knobs (`db_pool_size`, `db_max_overflow`, `db_pool_timeout_s`,
  `db_pool_recycle_s`, `db_statement_timeout_ms`, `db_slow_query_ms`). All
  default to the current hard-coded behaviour so existing callers are unaffected.
- No edits to other domains' models/repos. `app/db/models/__init__.py`,
  `app/db/repositories/__init__.py`, `composition.py` — untouched (the infra is
  opt-in; concrete repos adopt it when their owners choose).

## Status (delivered)

All eight phases are implemented, typed (mypy clean), and tested. ~2,640 lines of
infrastructure across 11 modules + ~1,400 lines of tests across 7 files.

Test counts (against `kinora_dblayer_test` on :5433):
- `test_db_engine_config.py` — 15 unit tests (config mapping, recorder).
- `test_db_retry.py` — 14 unit tests (classifier + retry loop).
- `test_db_query_helpers.py` — 13 unit tests (SQL shaping; compile-only).
- `test_db_migration_safety.py` — 14 unit tests (DDL SQL gen + linter + backfill).
- `test_db_generic_repo.py` — 13 integration tests (CRUD, soft delete, optimistic
  concurrency `StaleDataError`, UoW savepoints + serialization retry).
- `test_db_bulk_inspect.py` — 16 integration tests (bulk insert/upsert, EXPLAIN,
  expand/backfill/contract lifecycle, CONCURRENTLY index, pool health).
- `test_db_routing.py` — 5 integration tests (read/write split commit/rollback).

The full backend suite (`make test` with the isolated DB) is **1175 passed,
111 skipped** — the additive `config.py` settings + new modules break nothing.

Remaining roadmap (future, optional — not required for this phase):
- Wire `EngineRegistry`/`RoutingSessionFactory` into `composition.py` behind the
  existing seams when an owner opts in (kept out here to avoid touching the
  shared composition root beyond what's needed).
- A `/metrics` or `/ready` projection of `registry_health()` + `recent_slow_queries()`
  for the §12.5 observability panel (owned by the API domain).
- Adopt `GenericRepository` in concrete repos incrementally (each repo's owner).

## Test strategy

- Pure-unit tests (no infra) for: retry classification, query-helper SQL
  shaping, mixin column declaration, migration-safety SQL generation, engine
  config → URL/connect-args mapping. These run anywhere.
- Postgres-integration tests (skip when `KINORA_TEST_DATABASE_URL` unset, run
  against `kinora_dblayer_test` on :5433): generic repo CRUD + soft delete +
  optimistic version, UoW savepoints + serialization retry, EXPLAIN inspector,
  bulk upsert, online-index/backfill helpers, pool health snapshot.

Never touch the live `kinora` DB / redis db 0.
