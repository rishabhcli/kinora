# Query optimization platform — design & roadmap

Owner domain: **Database at scale, facet B — query optimization**, under
`backend/app/datascale/optimize/`. A self-contained *performance platform* the
app's hot paths can adopt. It composes the existing `app/db/inspect.py` EXPLAIN
inspector (the §12.5 observability dividend) and never edits another domain's
models/repos.

Read first: `kinora.md` §8.7 (caching & dedup — why a re-read costs nothing; the
content-hash discipline this platform generalises into result/dependency
caching), §12.3 (caching layers), §12.5 (observability). The §4.2 source-span
seek must be a btree `Index Scan`, never a `Seq Scan` — the index advisor and the
plan-regression guard exist to keep it that way.

## Design principles

1. **Lazy & side-effect-free imports.** Importing any module here opens no
   sockets and needs no network (mirrors `composition.py`, `db/engine.py`). All
   state lives in explicitly-constructed objects.
2. **Postgres-first, degrade gracefully.** EXPLAIN / `pg_stat_statements` paths
   use `app.db.inspect`, which raises a clear error off-Postgres. The *analysis*
   layers (fingerprinting, SQL shape, cost model, advisor heuristics, regression
   diff, N+1 detection, the workload generator) are pure Python and run anywhere,
   so the bulk of the platform is unit-testable with zero infra.
3. **Soundness over coverage for the rewriter.** The matview query-rewriter only
   rewrites a query when the rewrite is provably equivalent (a conservative
   containment check over a normalised relational fingerprint). When in doubt it
   declines and the original query runs — a wrong rewrite is never emitted.
4. **Dependency-precise invalidation.** The result cache tracks the *exact* set
   of tables a cached entry depends on, so a write to table T invalidates only
   entries that read T. This is §8.7's shot-hash discipline generalised from one
   render artifact to arbitrary query results.
5. **Deterministic tests.** Every analyzer is fed fixed inputs and asserted
   exactly; the workload generator is seedable; time/"now" is injectable.

## Module map (all under `app/datascale/optimize/`)

| Module | Responsibility | Phase |
|---|---|---|
| `errors.py` | Typed exception hierarchy (`OptimizeError`, `RewriteUnsound`, `RegressionDetected`, …). | 0 |
| `fingerprint.py` | SQL **normalisation + fingerprinting**: strip literals/whitespace/casing → a stable hash; extract referenced tables. The shared identity primitive. | 1 |
| `sqlshape.py` | Lightweight **SQL shape parser**: tables, columns, predicates, joins, aggregates, order/group of a SELECT — enough structure for matview matching, the advisor, and N+1 signatures, without a heavyweight parser dependency. | 1 |
| `matview.py` | **Automatic materialized views**: typed MV spec + dependency set + freshness policy, an in-memory registry, full + incremental-by-key refresh planning with a staleness clock, sound transparent query→MV rewrite, and Postgres DDL generation. | 2 |
| `resultcache.py` | **Query-result cache** with precise dependency-based invalidation: an LRU+TTL store keyed by fingerprint+param-hash, a table→keys dependency index, and table/row-scope invalidation fan-out. | 3 |
| `nplusone.py` | **N+1 detector** (groups repeated parameterised queries inside a request window, scores burst patterns) + **dataloader/batch-resolver** framework (async per-key batching with coalescing, dedup, one-tick scheduling). | 4 |
| `profiler.py` | **Hot-path profiler**: per-fingerprint stat aggregation (calls, total/mean/p95, rows, plan-cost — consumes `db.inspect`), flamegraph call-tree folding, and a ranked hot-path report. | 5 |
| `advisor.py` | **Index advisor**: candidate-index generation from predicates/joins/orderings, hypothetical what-if costing (a cost model; `hypopg` when present), rank + dedup + redundancy pruning → recommendations from a workload. | 6 |
| `regression.py` | **Plan-regression guard**: captured plan baselines (JSON), compare a fresh plan against baseline; flag node-shape changes, cost blow-ups, new Seq Scans — the CI gate that keeps §4.2 on an Index Scan. | 7 |
| `workloadgen.py` | **Synthetic workload generator**: seedable, emits realistic Kinora query shapes (book/shot/entity/continuity/source-span seeks) at chosen skew/volume to drive deterministic tests of every layer. | 8 |
| `platform.py` | `OptimizePlatform` facade wiring the layers together behind one opt-in object; nothing constructs it implicitly. | 9 |

## Soundness model for the rewriter (the load-bearing decision)

A matview `MV` defined by SELECT `S_mv` can answer a query `Q` only when `Q` is a
*sound projection/restriction* of `S_mv`:
- same base relation set (or a subset MV covers via its grouping keys),
- `Q`'s output columns ⊆ `MV`'s materialised columns,
- `Q`'s predicates ⇒ `MV`'s predicates (we only prove the decidable cases:
  identical predicate sets, or `Q` adds equality predicates on MV grouping keys),
- aggregates align (an MV that pre-aggregates `COUNT(*) GROUP BY book_id` can
  answer `COUNT(*) WHERE book_id = ?` but **not** `AVG(...)`).
Anything outside these provable cases → `rewrite()` returns `None` (decline), so a
rewrite is emitted only when equivalence holds. Tests assert both the accept and
the *decline* set, because the decline set is where correctness lives.

## Additive shared-file changes (documented per worktree rules)

- `app/datascale/__init__.py` — new package docstring naming the `optimize`
  subpackage. New package; additive.
- **No** edits to `app/db/`, `composition.py`, `core/config.py`, or any other
  domain. The platform consumes `app.db.inspect` read-only and is constructed
  only by callers that opt in. A sibling `app/datascale/sharding/` (facet A), if
  present, is untouched.

## Test strategy

- **Pure-unit (no infra, run anywhere):** fingerprint normalisation, SQL-shape
  parsing, matview rewrite accept/decline soundness, result-cache hit/miss +
  dependency invalidation fan-out, N+1 burst detection, dataloader batching/
  coalescing, profiler aggregation + flamegraph folding, advisor candidate
  generation + ranking + redundancy pruning, regression diff, workload generator
  determinism. The majority of the suite.
- **Postgres-integration (skip when `KINORA_TEST_DATABASE_URL` unset; run against
  `qopt_test` on :5433):** EXPLAIN-backed profiler ingestion, matview DDL apply +
  full/incremental refresh, regression guard against a real plan, advisor what-if
  against a real table. Never touches the live `kinora` DB.

## Status (delivered)

All ten phases are implemented, typed (mypy clean over the package + its tests),
ruff-clean, and tested. ~3,000 lines of platform code across 11 modules + ~2,000
lines of tests across 11 files.

| Phase | Module(s) | Status |
|---|---|---|
| 0 | `errors.py` | ✅ typed exception hierarchy |
| 1 | `fingerprint.py`, `sqlshape.py` | ✅ SQL normalise/fingerprint + structural shape parser |
| 2 | `matview.py` | ✅ MV defs, registry, staleness clock, full+incremental refresh planner, sound rewrite, DDL gen, executor |
| 3 | `resultcache.py` | ✅ LRU+TTL result cache, table + row-scope dependency invalidation |
| 4 | `nplusone.py` | ✅ N+1 burst detector + async DataLoader (per-tick coalescing) |
| 5 | `profiler.py` | ✅ per-shape stats (p95, plan-cost, seq-scan) + flamegraph fold; consumes `db.inspect` |
| 6 | `advisor.py` | ✅ candidate gen + cost-model what-if + rank + prefix-redundancy pruning |
| 7 | `regression.py` | ✅ plan snapshots, JSON baseline store, cost/seq-scan/node-shape diff guard |
| 8 | `workloadgen.py` | ✅ seeded, skewable Kinora-shaped query generator |
| 9 | `platform.py` | ✅ `OptimizePlatform` facade wiring every layer |
| 10 | `tests/test_qopt_integration_db.py` | ✅ Postgres-backed EXPLAIN/matview/regression/rewrite-equivalence |

Test counts:
- `test_qopt_fingerprint.py` — 12 unit tests.
- `test_qopt_sqlshape.py` — 21 unit tests.
- `test_qopt_matview.py` — 29 unit tests (rewrite accept *and* decline sets).
- `test_qopt_resultcache.py` — 21 unit tests (precise invalidation).
- `test_qopt_nplusone.py` — 17 unit tests (detection + dataloader batching).
- `test_qopt_profiler.py` — 12 unit tests (aggregation + flamegraph).
- `test_qopt_advisor.py` — 16 unit tests (candidates + ranking + pruning).
- `test_qopt_regression.py` — 18 unit tests (diff + baseline store).
- `test_qopt_workloadgen.py` — 12 unit tests (determinism + cross-layer compose).
- `test_qopt_platform.py` — 11 unit tests (facade end-to-end).
- `test_qopt_integration_db.py` — 7 Postgres-integration tests (run vs `qopt_test`
  :5433; skip when `KINORA_TEST_DATABASE_URL` unset).

**176 platform tests** (169 unit + 7 integration). The full backend suite with the
additive package is **4087 passed, 678 skipped** — nothing else breaks.

The integration suite caught a real rewriter bug: the column reconstruction for an
aggregate query (`SELECT book_id, count(*)`) dropped the aggregate column because
the parser carries it as an *aggregate*, not a *column*. Fixed by projecting ``*``
for aggregate-MV rewrites (the MV materialises columns in projection order), and
the integration test now asserts the rewritten SQL returns identical rows to the
original.

## Remaining roadmap (future, optional)

- A live `hypopg` what-if path in `advisor.py` (the cost model is authoritative
  today; `whatif_with_hypopg()` is the seam).
- Wire `OptimizePlatform.observe` into an instrumented engine listener so the
  profiler/detector feed themselves (kept opt-in here; the API/db domain owns the
  engine seam).
- A `/metrics` projection of `OptimizePlatform.snapshot_stats()` for the §12.5
  panel (owned by the API domain).
- Persist matview definitions + baselines into the repo as reviewed fixtures for a
  CI plan-regression gate on the §4.2 source-span seek.
