# ML dataset + trace pipeline (`backend/app/mlplatform/datasets/`)

**Facet A** of Kinora's self-improvement ML platform: the **data foundation**.
It ingests the crew's agent run-traces (prompt + input + output + QA verdict +
director edits) through a **read-only seam** and turns them into a *versioned,
immutable* dataset store with lineage, dedup, PII scrubbing, leak-free
stratified train/val/test splitting, weak-supervision labeling, dataset diffing
+ stats + drift checks, and export adapters (JSONL / columnar) that the sibling
alignment + serving facets consume.

It is layered **over** the existing planes without editing them:

- The **trace seam** reads `app.llmops.tracing.RunTrace` (+ the Critic's QA
  record and director edits, joined in by read-only callbacks) — it never
  imports the agents or any write path.
- The agents' `QARecord` (§9.5) and director edits (§5.4) are *mirrored* into
  package-local value objects, so the data plane is decoupled from agent churn.

Cites: **kinora.md §10** (prompt contracts — what a trace's prompt/input/output
mean), **§13** (metrics & the eval harness — the QA verdict + the honest,
leak-free, reproducible split this pipeline must produce), and §5.4 (director
edits as the strongest supervision signal).

Design constraints (per the worktree brief):
- **Stay inside this NEW package**; **additive-only** on shared files, documented
  below.
- Every input is a **fake / in-memory source** — zero model calls, zero credits,
  `KINORA_LIVE_VIDEO` OFF (the pipeline is pure data transforms).
- New DB tables ship as **one Alembic migration** (`mldata_0001`) with a UNIQUE,
  domain-prefixed revision id, on the current head at branch time
  (`9f3c7a1e2b4d`, the llmops head — the run-trace table the pipeline reads is
  created there).
- **No FK into existing tables** — a frozen dataset must outlive a book/user
  deletion (loose `book_id` / `session_id`, exactly like the llmops run trace +
  the analytics event log).

## The pipeline (stage order — each stage's output is the next's input)

```
TraceSource ─▶ ingest ─▶ scrub ─▶ dedup ─▶ label ─▶ split ─▶ versioned Dataset
              (RawTrace   (PII /    (exact +  (weak-     (leak-free,   (+ export
               → Trace-    secret   near-LSH  supervision  stratified)  adapters)
               Example)    redact)  collapse) consensus)
```

- **ingest** — `RawTrace → TraceExample`. Maps `prompt_key → AgentRole`, infers
  the `TaskType` (a trace carrying a QA verdict / director edit is `PREFERENCE`
  fuel; a clean trace is `SFT`), projects the QA + edit signals, derives a
  default scalar reward (penalised by edit count), and applies the drop policy
  (errors / cache hits / empty outputs). Idempotent: the example id is a hash of
  the trace identity.
- **scrub** — deterministic PII / secret redaction (email/phone/card/SSN/IP/API
  keys/JWT), MASK or salted-HASH per rule. Runs **before** dedup so two PII-
  differing-but-otherwise-identical traces collapse. Idempotent → stable hash.
- **dedup** — exact collapse on the provenance-free `content_hash`, then
  near-duplicate collapse via MinHash-LSH + a Jaccard confirm; the
  highest-value example survives each cluster (QA-passed > higher reward > more
  edits > newer).
- **label** — Snorkel-style weak supervision: bundled labeling functions vote
  `good`/`bad`/abstain; a label model (weighted majority vote with agreement-
  estimated LF accuracies) produces a consensus `quality` label + confidence.
- **split** — leak-free at **group granularity** (group = book by default), so
  no book straddles train/test; stratified per role/QA; deterministic from a
  seed. The `SplitReport` re-derives the no-leak invariant from the *result*.

Each stage commits an **immutable, content-addressed `DatasetVersion`** under the
same name, so `registry.history(name)` *is* the build's audit trail and
`registry.lineage(v)` reconstructs the full provenance DAG (a merge can have two
parents).

## Module map (all under `app/mlplatform/datasets/`)

| Module | Responsibility |
|---|---|
| `errors.py` | Exception hierarchy (`MLDataError` + subclasses). |
| `contracts.py` | The load-bearing contracts: `TraceExample`, `Dataset`, `RawTrace`, `TraceSource` (Protocol), `QAVerdict`/`DirectorEdit`, `AgentRole`/`TaskType`/`Split`, canonical hashing. |
| `sources.py` | Read-only seams: `LLMOpsTraceSource` (adapts `app.llmops` trace store + QA/edits joins) + `InMemoryTraceSource` (fake). |
| `ingest.py` | `RawTrace → TraceExample` normalization, role mapping, task inference, reward derivation, drop policy. |
| `scrub.py` | Deterministic PII / secret scrubbing (mask / salted-hash rules). |
| `dedup.py` | Exact + near-duplicate (MinHash-LSH) dedup, best-representative survival. |
| `splitting.py` | Leak-free, stratified, deterministic train/val/test splitting. |
| `labeling.py` | Labeling functions + the weak-supervision label model. |
| `stats.py` | One-pass descriptive stats (distributions, reward/length summaries, entropy). |
| `drift.py` | Distribution drift (PSI / JS divergence / KS) between two versions, with severities. |
| `diff.py` | Structural diff (added / removed / changed examples + field-level deltas). |
| `versioning.py` | The immutable, content-addressed version store + lineage DAG + `DatasetRegistry`. |
| `export.py` | JSONL (record / SFT / preference) + columnar (dict-of-arrays + CSV) adapters. |
| `pipeline.py` | `DatasetPipeline` — orchestrates the staged build, commits every stage. |
| `sampling.py` | Deterministic subsample / class-balance (under/over/target) / weighted (reward) / stratified sampling. |
| `filtering.py` | Composable quality predicates + report, curriculum ordering, gold/silver/bronze tiers, `golden_subset`. |
| `service.py` | `DatasetService` — the façade the composition root + API wire to (build / export / stats / drift / diff / lineage + `derive_*`). |
| `store.py` | DB-backed durable mirror (`DatasetVersionStore`) over the 3 `mldata_*` tables. |
| `cli.py` | A self-contained, offline CLI (`python -m app.mlplatform.datasets.cli build/export/inspect`) — does **not** touch the shared `app.cli` surface. |

### Derived versions (post-build reshaping with lineage)

`DatasetService.derive_filtered` / `derive_golden` / `derive_balanced` commit a
new `FILTER`-op version that is a **child** of a built version, so a reshaped
training set (the QA-passed golden subset, a class-balanced cut, a custom
predicate) keeps a lineage edge back to the corpus it came from. The sampling /
filtering primitives are also exposed directly for the sibling facets to compose
their own datasets.

## Cross-facet contracts (what the sibling facets import)

These are the *stable* shapes the alignment (reward modelling) and serving
(routing) facets consume; they are defined here and re-exported from the package
`__init__`:

- **`TraceSource` / `RawTrace`** — the read-only ingest seam. A facet that has its
  own trace origin implements `TraceSource` (an `iter_raw` + `count`); the data
  plane needs nothing else.
- **`TraceExample`** — one immutable training example: prompt identity, input,
  output, optional `QAVerdict`, `DirectorEdit`s, scalar `reward`, `labels` /
  `weak_labels`, a `split`, provenance, and a provenance-free `content_hash`.
- **`Dataset`** — an immutable, ordered collection of `TraceExample` with a
  content hash and a small algebra (`filter` / `map` / `concat` / `by_split` /
  `by_role` / `by_task`).
- **`DatasetService`** — `build` / `build_from_llmops` / `export_jsonl` /
  `export_columns` / `export_csv` / `training_feed` / `stats` / `drift` / `diff` /
  `lineage` / `tag`. A sibling facet pulls a split's training feed via
  `training_feed(ref, shape=ExportShape.SFT|PREFERENCE, split=Split.TRAIN)`.
- **Export shapes** — `RECORD` (lossless audit), `SFT` (`messages`+`completion`
  pairs, good-only by default), `PREFERENCE` (`prompt`/`chosen`/`rejected` pairs
  contrasted by reward within a group). The reward facet trains its RM on
  `PREFERENCE`; an SFT facet imitates the `good` behaviours.

## DB tables (one migration, `mldata_0001`, down_revision `9f3c7a1e2b4d`)

- `mldata_dataset_versions` — one row per committed, content-addressed version
  (operation + JSONB stats snapshot + op-params + tags); the PK *is* the
  content-addressed `version_id`. Immutable: inserted once, never updated.
- `mldata_examples` — the frozen examples of a version (full record JSONB + hot
  filter columns role/task/split/content_hash), UNIQUE `(version_id, example_id)`,
  loose `book_id` / `session_id`.
- `mldata_lineage_edges` — the parent→child edges of the version DAG.

All additive; no FK into existing tables. The store *flushes, never commits* (the
unit-of-work boundary owns the transaction); infra-bound tests skip cleanly when
`KINORA_TEST_DATABASE_URL` (the isolated `mldata_test` :5433) is unset.

## Shared-file additive changes (additive-only, documented here)

- `app/db/models/__init__.py`: imported + re-exported the 3 new ORM models
  (`MLDataDatasetVersion`, `MLDataExample`, `MLDataLineageEdge`) so Alembic
  autogenerate + `create_all` see the tables. No existing import touched.
- `app/composition.py`: added a private `_dataset_service` field and a lazy
  `Container.dataset_service` property building `DatasetService` (pure + offline,
  in-memory registry; constructs nothing until reached for). Mirrors the existing
  `llmops` property exactly; no change to existing wiring.

## Determinism + safety invariants

- **Idempotent ingest + scrub**: re-running over the same trace yields the same
  example id and the same scrubbed content hash, so re-builds are no-ops.
- **Content addressing**: `content_hash` ignores provenance, so two semantically
  identical examples dedup to one; the dataset hash is over the ordered example
  hashes + name.
- **Immutable versions**: overwriting a committed version with different content
  raises `ImmutabilityError`; re-committing identical content returns the
  existing version.
- **Leak-free splits**: the split is verified against its own *result*; a leak
  would raise `SplitError` (the report's `leak_free` is always `True` for a
  correct split).
- **No PII in a frozen version**: scrubbing runs before any version that leaves
  the ingest stage, so committed examples + every export are redacted.

## Test coverage

`tests/mlplatform/` — 115 pure unit tests (zero infra) over every stage +
contracts + sampling + filtering + the CLI + the end-to-end pipeline/service,
plus 4 DB-store tests that skip without `KINORA_TEST_DATABASE_URL`. `make lint`
(ruff + mypy) is green over the package, the model, the migration, and the
touched shared files.
