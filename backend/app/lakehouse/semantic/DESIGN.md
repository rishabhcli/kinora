# DESIGN.md — Lakehouse facet C: the semantic / metrics layer

> Living roadmap for the **governed metrics-as-code semantic layer**
> (`backend/app/lakehouse/semantic/`). All work here is **additive**: a brand-new
> package plus brand-new test files. **No shared/existing file is edited.**

## Scope & ownership

Owned (free to create/edit): everything under `backend/app/lakehouse/`, and the
`backend/tests/test_lakehouse_semantic_*.py` + `tests/lakehouse_fixtures.py` test
files. The package is the **C facet** of the lakehouse: a dbt-MetricFlow /
LookML-shaped semantic + metrics layer compiled to query plans against the
warehouse `QueryEngine` (sibling **facet A**) with a SQL fallback.

**Additive shared-file changes: none.** The layer composes against facet A
through a structural `QueryEngine` Protocol (`engine.py`), so it neither imports
nor edits the sibling package; until facet A lands on disk the bundled
`InMemoryEngine` is a complete, dependency-free reference engine.

## Goal (task brief + kinora.md §13)

A governed metrics-as-code layer:
1. a **declarative semantic model** (entities/dimensions/measures/joins);
2. a **metric-definition language** (simple, ratio, derived, cumulative,
   time-comparison; metric- and measure-level filters);
3. a **deterministic compiler** lowering a query to an engine-agnostic plan,
   with a **SQL fallback** renderer;
4. a **self-serve query API** (group-by / filter / time-grain) with
   **governance**: metric allow/deny, column mask/deny, row-level filters;
5. **result caching** (plan-fingerprint + access-scope keyed) + a
   **materialization advisor**;
6. **metric lineage** + a searchable **metrics catalog**;
7. the **§13 KPIs** defined as code (CCS, accepted-footage efficiency, regen
   rate, buffer health, budget burn) — verified to agree with the authoritative
   pure math in `app.eval.metrics`.

Hard rules honoured: zero credits, `KINORA_LIVE_VIDEO` OFF (the budget-burn KPI
reads 0 because no live spend happens), deterministic compiler tests over an
in-memory engine, no commit/push.

## Module map (`backend/app/lakehouse/semantic/`)

| Module | Responsibility |
|---|---|
| `types.py` | data types, time grains, aggregations, comparison ops, the pure filter AST (+ evaluator), ordering |
| `model.py` | declarative `SemanticModel` (entities/dimensions/measures/joins) |
| `metrics.py` | the metric DSL: `Simple`/`Ratio`/`Derived`/`Cumulative`/`TimeComparison` |
| `registry.py` | `SemanticGraph` — validate refs, build the metric DAG (cycle-checked) + join graph (BFS shortest path), expand base measures |
| `query.py` | `MetricQuery` — the self-serve request (metrics, group-by, filters, time grain/window, order/limit) |
| `plan.py` | the compiled, engine-agnostic plan IR + a stable `fingerprint()` |
| `compiler.py` | lower `MetricQuery` → `QueryPlan` (join resolution, fan-out safety, filtered-measure dedup, grain validation, post-agg ordering) |
| `engine.py` | the `QueryEngine` **Protocol** (facet A seam) + `InMemoryEngine` reference |
| `executor.py` | run the plan + fold post-agg computations (ratio/derived/cumulative/time-comparison) → `MetricResult` |
| `arith.py` | a safe recursive-descent arithmetic interpreter for derived exprs (no `eval`) |
| `sql.py` | the SQL fallback — render a plan to **parameterised** Postgres SQL |
| `governance.py` | `Principal` / `AccessPolicy` / `GovernanceEngine` — metric/column/row access control |
| `cache.py` | TTL+LRU `InMemoryResultCache` keyed by plan fingerprint + access scope |
| `advisor.py` | `MaterializationAdvisor` — observe plans, recommend additive pre-aggregations + coverage |
| `lineage.py` | `LineageGraph` — upstream/downstream + physical-column lineage |
| `catalog.py` | `MetricsCatalog` — search/browse/facet the metric + dimension surface |
| `kpis.py` | the §13 KPIs as code (+ catalog tags) |
| `loader.py` | declarative dict/YAML → validated `SemanticGraph` (metrics-as-code authoring) |
| `serialize.py` | JSON-safe DTOs (table / chart-series / explain / catalog / lineage) |
| `service.py` | `SemanticLayer` — the composed facade: govern → compile → cache → execute → mask |
| `factory.py` | `build_kinora_layer` — wire the §13 KPIs onto the render-telemetry star schema |

## Key design decisions

- **Two-stage plan.** The compiler splits a query into (1) one grouped
  `AggregationPlan` the engine runs and (2) a list of pure post-aggregation
  `MetricComputation`s the layer folds over the aggregate rows. Only stage 1
  touches data; ratios/derived/cumulative/time-comparison are pure arithmetic.
- **Facet-A seam = a Protocol.** `engine.py:QueryEngine` is the entire contract
  (`execute_aggregation(plan) -> AggregateResult`); the layer compiles against it
  with no build dependency on facet A.
- **Determinism.** Same graph + same query ⇒ byte-identical plan + fingerprint
  (frozen dataclasses, canonical JSON encoding). The fingerprint is the cache /
  advisor / lineage key.
- **Fan-out safety.** Cross-model aggregation is only allowed across declared
  **many-to-one** joins (the compiler raises otherwise), the property additive
  rollups and cumulative metrics rely on.
- **Filtered-measure dedup.** Two metrics over the same measure with different
  filters compile to two distinct, hashed aggregate columns; identical uses share
  one.
- **Governance composes into the plan.** Row policies are conjoined onto the
  user's filters *before* compile (enforced inside the aggregation), and the
  cache key folds the access scope so principals never share filtered/masked
  results.

## Test coverage

`test_lakehouse_semantic_{types,compile,plan,sql,service,advisor_cache,lineage_catalog,kpis,loader,serialize}.py`
— 122 deterministic tests over the in-memory engine. The KPI suite cross-checks
the declarative metrics against `app.eval.metrics` (the authoritative §13 math).

`make lint` (ruff + mypy, strict `disallow_untyped_defs`) and the lakehouse test
selection are green.

## Remaining roadmap (future facets, not blocking)

- A SQLAlchemy-backed `QueryEngine` that executes `render_sql()` against Postgres
  (the SQL renderer is done + tested; only the connection-runner is pending).
- Persisted/distributed result cache (Redis) behind the `ResultCache` Protocol.
- A FastAPI route module exposing `SemanticLayer.query` + the catalog/lineage
  DTOs (the `serialize.py` payloads are route-ready).
- Advisor → DDL generation for recommended materializations + incremental refresh.
