# Backend search & indexing service — DESIGN.md (living roadmap)

> Owner: search-subsystem agent. Package: `backend/app/search/` + `api/routes/search.py`.
> This is the **server-side** search engine — distinct from any client-side discovery UI.
> Reads kinora.md §8 (memory/canon): search *complements* the canon, it does not
> duplicate it. The canon is the authoritative versioned graph + episodic vector
> store; search is a denormalized, query-optimised projection of book content
> (books, pages, scenes, beats, canon entities, shots) for free-text + semantic
> lookup across a library.

## Why this is separate from `app/memory/retrieval.py`

`retrieval.py` is the *fine re-rank* (MMR / hybrid / knapsack packing) applied to
the canon slice the agents consume for a single beat — it never indexes a corpus.
This subsystem is a **corpus search engine**: it builds and maintains an inverted
index + ANN index over every searchable document in the library and answers
ad-hoc queries (BM25 + vector hybrid with reciprocal-rank fusion, facets,
highlighting, typo tolerance). It *reuses* `retrieval.cosine` and the embeddings
provider; it does not re-implement the per-beat retrieval policy.

## Design principles

1. **Pluggable index** behind one `SearchIndex` protocol. Two real backends:
   - `InMemoryIndex` — full BM25 + cosine + RRF, highlighting, facets. Used by
     tests and by any offline/zero-infra path. No DB, no network.
   - `PostgresIndex` — Postgres FTS (`tsvector`/`websearch_to_tsquery`) +
     pgvector (`<=>`) hybrid fused with RRF. The production backend.
2. **Deterministic, offline-testable.** Embeddings come through the `Embedder`
   protocol; tests inject a fake embedder. No live calls, zero credits.
3. **Versioned indexes + aliases.** `kinora_v1`, `kinora_v2`, … with an alias
   (`kinora_current`) pointing at the live version. Bulk reindex builds a new
   version then atomically swaps the alias → zero-downtime reindex.
4. **Additive only on shared files.** New table `search_documents` (+ migration
   chaining the current head), new route registered in `ROUTERS`, new settings
   on `Settings`, optional wiring on the `Container`. Everything else is new
   files inside `app/search/`.

## Milestones

- [x] **M1 — analysis & query parsing** (`analyzer.py`, `query.py`)
  Tokenizer, stopwords, Porter-style stemmer, synonyms, edit-distance typo
  tolerance; query grammar: phrases (`"..."`), boolean (`AND/OR/NOT`, `-term`),
  field filters (`field:value`), facet selectors, ranges.
- [x] **M2 — documents & ranking** (`documents.py`, `ranking.py`)
  `SearchDocument` schema, doc kinds, BM25 scorer, reciprocal-rank fusion,
  score normalization.
- [x] **M3 — highlighting** (`highlight.py`)
  Best-window snippet extraction + `<mark>` term highlighting, multi-field.
- [x] **M4 — index abstraction + in-memory backend** (`index.py`,
  `memory_backend.py`) — full hybrid search, facet aggregation, suggestions.
- [x] **M5 — Postgres backend** (`postgres_backend.py`, model + migration)
  FTS + pgvector hybrid with RRF over the `search_documents` table.
- [x] **M6 — indexing pipeline** (`pipeline.py`)
  Project books/pages/scenes/beats/entities/shots → `SearchDocument`s;
  incremental upsert + bulk reindex.
- [x] **M7 — versioned aliases** (`alias.py`) — version registry + atomic swap.
- [x] **M8 — service + query API** (`service.py`, `api/routes/search.py`)
  Orchestrate parse → search → highlight → facet; REST surface.
- [x] **M9 — Container wiring + settings** (additive).
- [x] **M10 — tests** — unit (offline, in-memory) + integration (isolated DB).

## Status (all milestones M1–M10 complete)

- `make lint` (ruff + mypy over `app` + `tests`): GREEN.
- Unit suite (offline, no infra): 96 search tests green; full repo unit suite
  unaffected (1137 passed, infra-bound tests skip).
- Integration (isolated DB `kinora_search_test` + `kinora_conflict_test`, redis
  db 15): Postgres FTS/vector/RRF (18) + API route (10) green; migration
  `s1a2b3c4d5e6` upgrades + downgrades cleanly on the full chain; `alembic heads`
  is a single head.

## Remaining roadmap (future phases)

- Learned-ranking / click feedback signals folded into RRF weights.
- Per-user library scoping baked into the Postgres FTS index partitions (today
  the library-wide route over-fetches then filters to the owned set, fail-closed;
  a single owned `book_id` pushes the scope into the index).
- Cross-encoder rerank stage over the top-N fused candidates.
- Spelling-correction "did you mean" surfaced in the API response (the service
  computes it; the route can echo it).
- A background worker tick to keep the index fresh on ingest/canon-edit/shot-accept
  (the incremental `index_book` is ready; only the trigger wiring remains).

## Additive shared-file changes

- `app/core/config.py` — search-related settings (additive block).
- `app/db/models/__init__.py` — register `SearchDocument` (additive import).
- `app/api/routes/__init__.py` — register `search.router` in `ROUTERS` (additive).
- `app/composition.py` — optional `search_*` seam + `build_search_service()` (additive).
- New Alembic migration `s1a2b3c4d5e6_search_documents.py` chaining head `a1b2c3d4e5f6`.
