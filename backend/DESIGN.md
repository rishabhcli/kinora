# Server-side recommendations engine — DESIGN.md

A living roadmap for `backend/app/recommendations/` — the **server** recsys that
ranks which *books* a reader should watch next (distinct from any client-side
discovery UI). It reuses the embedding space and retrieval math the memory layer
already ships (kinora.md §8) rather than duplicating them.

## Why this exists

Kinora turns books into films; a reader who finishes one wants the next. The
recsys answers "what should I read next?" from three independent signals and
blends them:

1. **Content-based** similarity over book/canon embeddings (the same 1152-d
   shared image+text space from §8 — `tongyi-embedding-vision-plus`). "More
   books like the ones you read."
2. **Collaborative filtering** — item-item + user-item over an interaction
   matrix. "Readers like you also watched X."
3. **A per-user taste vector** with recency decay — a single dense vector that
   summarizes a reader's evolving taste, refreshed every interaction.

These feed a **candidate-generation → scoring → re-ranking** pipeline with
diversity (MMR, reused from `app.memory.retrieval`), business-rule boosts, and
**cold-start** fallbacks (popularity + content). Every result carries an
**explainable reason** ("because you read X"). An **offline eval harness**
(precision@k, recall@k, nDCG, MAP, coverage, diversity) scores the ranker over
synthetic interaction logs — zero credits, deterministic.

## Design principles

- **Pure & deterministic core.** All the math (similarity, CF, taste vectors,
  ranking, metrics) is pure functions over plain numbers/vectors so the unit
  suite pins it against hand-computed values with **fake/one-hot embeddings** —
  no network, no credits, runs anywhere.
- **Reuse, don't duplicate.** `cosine`, `normalize`, `mmr_rerank`, `Scored`,
  `rank_by` come from `app.memory.retrieval`; the `Embedder` protocol from
  `app.memory.interfaces`; the embedding dim from `app.providers.embeddings`.
- **Staged pipeline.** Candidate generation (cheap recall, possibly many
  sources) → scoring (blend the signals) → re-ranking (MMR diversity + business
  boosts + dedup). Mirrors the canon-slice retrieval policy (§8.4).
- **Explainability is first-class.** Each scored item records *which* signals
  fired and *why*, surfaced as natural-language reasons.
- **Additive only on shared files.** New tables → a fresh Alembic migration on
  the current head (`a1b2c3d4e5f6`), unique revision id. `config.py`,
  `composition.py`, `db/models/__init__.py`, `api/routes/__init__.py` get
  additive-only edits.

## Module map (`backend/app/recommendations/`)

| Module | Responsibility |
|---|---|
| `types.py` | Core dataclasses: `Interaction`, `InteractionKind`, `BookFeatures`, `Candidate`, `ScoredCandidate`, `Recommendation`, `Reason`, weights/config |
| `similarity.py` | Vector/content similarity over book embeddings; content-based candidate generation; centroid math |
| `taste.py` | Per-user taste-vector model with recency (exponential time) decay + interaction-kind weighting |
| `collaborative.py` | Interaction matrix, item-item & user-item CF (cosine over co-occurrence), neighbor scoring |
| `coldstart.py` | Popularity model + content fallback for users/items with no history |
| `reasons.py` | "Because you read X" reason synthesis → natural language |
| `ranker.py` | Blended scorer (content + CF + taste + popularity + boosts), MMR diversity re-rank, dedup, reason synthesis |
| `engine.py` | The orchestrator: candidate-gen → score → re-rank stages, wiring the sub-models; pure given injected feature/interaction stores |
| `synthetic.py` | Deterministic synthetic interaction-log + feature generator for eval/tests |
| `eval.py` | Offline eval harness: precision@k, recall@k, nDCG@k, MAP, coverage, diversity, novelty over synthetic logs + leave-one-out splitter |
| `store.py` | DB-backed feature/interaction repositories + the async `RecommendationService` |

## Data model (new tables — own migration, `down_revision = a1b2c3d4e5f6`)

- `book_interactions` — append-only log: `(user_id, book_id, kind, weight,
  dwell_s, created_at)`. The CF matrix + taste vectors are folded from this.
- `book_features` — per-book cached feature row: `embedding` (1152-d, the canon
  centroid), `popularity`, `tag` vector, `genre`, computed `updated_at`.
- `user_taste_vectors` — per-user cached dense taste vector + the decay
  bookkeeping (`last_event_at`, `event_count`), so taste is incremental.

## Roadmap / phases

- [x] **P1 Foundation** — package skeleton, `types.py`, `similarity.py`, reuse wiring, first tests.
- [x] **P2 Taste model** — recency-decayed per-user taste vector; tests pin decay math.
- [x] **P3 Collaborative filtering** — interaction matrix, item-item + user-item CF; tests.
- [x] **P4 Cold-start** — popularity + content fallback; tests.
- [x] **P5 Blended ranker + MMR + boosts + reasons** — the re-rank stage; tests.
- [x] **P6 Engine orchestrator** — candidate-gen → score → re-rank; end-to-end pure tests.
- [x] **P7 Offline eval harness** — precision@k / recall@k / nDCG / MAP / coverage / diversity / novelty + synthetic logs; tests.
- [x] **P8 DB layer** — ORM tables + Alembic migration on head; additive `db/models/__init__.py`.
- [x] **P9 Service + API** — async `RecommendationService` over the warehouse; FastAPI route; additive `composition.py` + `api/routes/__init__.py`.
- [ ] **P10 (future)** — learning-to-rank weight tuning from logged outcomes; A/B harness; session-context recs; nightly `book_features` backfill job; cached-taste-vector read path in the service hot loop.

## Tests

- **Unit (no infra, deterministic, zero credits):** `tests/test_recs_{types,similarity,taste,collaborative,coldstart,ranker,engine,eval}.py` — 67 tests pinning the pure math (decay half-life, CF cosines, blend arithmetic, MMR ordering, the ranking metrics) and an end-to-end **engine-beats-popularity/random** proof on a seeded synthetic dataset (nDCG/recall/MAP/coverage all dominate the baselines).
- **Integration (isolated `kinora_recs_test` on :5433, skips cleanly when unset):** `tests/test_recs_store.py` — 9 tests over the DB repos + `RecommendationService` + the FastAPI route, with the deterministic `FakeEmbedder` injected (no network). Run with:
  ```
  KINORA_TEST_DATABASE_URL=postgresql+asyncpg://kinora:kinora@localhost:5433/kinora_recs_test \
  KINORA_TEST_REDIS_URL=redis://localhost:6379/15 \
  KINORA_TEST_S3_ENDPOINT_URL=http://localhost:9000 \
  backend/.venv/bin/pytest tests/test_recs_store.py -q
  ```
- Migration verified: full chain `alembic upgrade head` ends at `r3c8a1d7f2b9`; `downgrade -1` drops the three tables cleanly.

## Shared-file changes (additive only)

- `backend/app/core/config.py` — recsys tuning knobs (`recs_*`) with safe defaults.
- `backend/app/db/models/__init__.py` — import + export the three new models.
- `backend/migrations/versions/<rev>_recommendations_engine.py` — new tables, `down_revision = "a1b2c3d4e5f6"`.
- `backend/app/composition.py` — lazy `recommendation_service` seam (additive).
- `backend/app/api/routes/__init__.py` — mount the recs router (additive).
