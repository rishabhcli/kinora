# Feature Store — `app/lakehouse/features/` (lakehouse facet B)

An online + offline **feature platform** in the Feast lineage, built natively into
the Kinora backend and tuned to its conventions (string ids, the §8 shared
1152-d embedding space, the "recall the relevant slice, never the whole log"
discipline of §8.4). It is the typed feature layer that the existing
recommendations engine (`app/recommendations/`) and any future ML consume.

## Why this is distinct from `app/recommendations/`

The recommendations engine *is a model*: candidate-generation → scoring →
re-ranking, with its own warehouse tables (`book_interactions`, `book_features`,
`user_taste_vectors`). The feature store is the **platform that serves features to
models like it**: a definition registry, point-in-time-correct training joins,
low-latency online serving, and offline/online parity + skew + freshness + lineage.
A recsys reads features; it is not itself a feature store. The two compose — a
`FeatureService` ("recsys_v1") can pin the exact feature set + column order a
ranking model trains and serves on, so training/serving cannot silently drift.

## Why this is distinct from lakehouse facet A

Facet A (a sibling lakehouse facet, possibly built in parallel) owns the columnar
`Table` + `QueryEngine`. The feature store **consumes** facet A's engine to read a
view's historical rows when it is present (`engine_seam.py` — a runtime-checkable
`Protocol`, never a hard import), and falls back to a built-in in-memory store
when it is absent. So this package is always self-contained, infra-free, and
unit-testable, and upgrades transparently to the warehouse when facet A lands.

## The point-in-time correctness guarantee (the crown jewel)

`pit.py` is the anti-label-leakage core. For a training row labelled at time `r`,
every joined feature value must have been known **at or before `r`**, and within
the feature view's TTL window. The join is a backward as-of merge per entity key:

    pick the row with the greatest event time t such that  (r - ttl) < t <= r
    (tie-break equal t by latest arrival, then stored order)

This enforces both **causality** (no future value → no leakage) and **recency**
(no value past its TTL → §8.5 "scope a fact to the interval where it was true").
The same TTL-aware pick is reused for online materialisation, so offline/online
parity is structural, then verified empirically (`parity.py`). The invariants are
**property-tested with Hypothesis** against a brute-force oracle over thousands of
generated histories (`tests/lakehouse/features/test_pit_properties.py`):
no-leakage, TTL bound, as-of maximality, order-independence/determinism, and
monotone revelation.

## Module map

| Module | Responsibility |
|---|---|
| `types.py` | The contract value objects: `ValueType`, `Entity`, `FeatureSpec`, `FeatureSource`, `Transformation`, `FeatureView`, `OnDemandFeatureView`, `FeatureService`, `FeatureRef`. Frozen, validated, content-fingerprintable. |
| `rows.py` | `FeatureRow` / `EntityRow` / `Frame` — the dependency-free tabular value objects (no pandas) the stores + join speak. |
| `registry.py` | `FeatureRegistry`: register entities/views/on-demand-views/services, **content-addressed versioning** (identical definition → same version, any change → new version), reference resolution + validation, on-demand transform evaluation. |
| `pit.py` | `point_in_time_lookup` / `point_in_time_join` — the TTL-aware backward as-of merge. Pure + deterministic. |
| `engine_seam.py` | The structural `Protocol` onto facet A's `Table` / `QueryEngine` + the row adapter. Loose coupling, no hard import. |
| `offline_store.py` | The historical store (`OfflineStore` protocol + `InMemoryOfflineStore` + `EngineOfflineStore`), `get_historical_features` (training-set generation), `latest_rows` (materialisation source). |
| `online_store.py` | The serving store (`OnlineStore` protocol + `InMemoryOnlineStore` + `RedisOnlineStore`), `get_online_features`. Redis key TTL = the view TTL (defence-in-depth staleness). |
| `materialization.py` | `materialize` / `materialize_view` — offline→online push of the latest value, with a `MaterializationResult` (coverage) for monitoring. |
| `parity.py` | `check_parity` (offline vs online agreement per key) + `detect_skew` (training-serving distribution drift: PSI for numeric, L-infinity for categorical). |
| `freshness.py` | TTL + SLA freshness classification + report (the serving twin of the offline TTL). |
| `lineage.py` | The source→view→on-demand→service lineage graph; `upstream` (audit) + `downstream` / `affected_services` (change blast radius). |
| `monitoring.py` | `FeatureMonitor` — a thread-safe, Prometheus-free telemetry aggregator (hit rate, materialisation, parity/skew/freshness per view). |
| `on_demand.py` | The request-time + streaming computation seam: `apply_on_demand`, `push_stream_rows`, `days_since`. |
| `store.py` | `FeatureStore` — the façade tying it all together (define → ingest → train → materialise → serve → validate). Infra-free by default. |
| `serde.py` | Feature-definition ↔ JSON for the durable registry snapshot (exact round-trip). |
| `db_models.py` | The additive `feature_store_*` ORM tables (durable offline history, registry snapshot, materialisation log). |
| `db_repo.py` | Async repositories (flush-not-commit) + `DbOfflineStore` (async `hydrate` → sync `source_rows`, the recsys's up-front-load pattern). |

## Additive shared-file changes (the only files touched outside this package)

* `backend/app/db/models/__init__.py` — **additive import** of the three
  `feature_store_*` models (registers them on `Base.metadata` for Alembic
  autogenerate + `create_all`) and their names appended to `__all__`. This is the
  established single table-registration entry point; no existing line changed.
* `backend/migrations/versions/featstore_0001_feature_store_tables.py` — **new**
  Alembic migration (unique id `featstore_0001`) chaining off the recommendations
  head `r3c8a1d7f2b9`. Creates only the three new tables; touches nothing else.
  (The repo already has a multi-head history from parallel work; this adds a leaf,
  applied by `alembic upgrade heads`.)

No production code imports the feature store yet — it is a self-contained platform
ready to be wired into the composition root / recommendations service when the
consuming model opts in.

## Testing

`tests/lakehouse/features/` — 84 tests: the Hypothesis property suite for
point-in-time correctness, plus example-based suites for the registry/versioning,
offline training joins (TTL, leakage, tie-break, multi-entity), online +
materialisation, parity + skew (PSI/L-infinity), freshness + lineage + monitoring,
the on-demand/streaming seam, serde round-trip, and the `FeatureStore` façade
end-to-end. Infra-gated DB tests for the durable spine skip cleanly with no
Postgres (matching the repo's `KINORA_TEST_DATABASE_URL` convention).
