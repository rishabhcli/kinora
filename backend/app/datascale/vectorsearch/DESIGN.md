# `app.datascale.vectorsearch` — scalable ANN vector-search service (DESIGN)

> A from-scratch, NumPy-only approximate-nearest-neighbour stack: HNSW graph,
> product + scalar quantization, a sharded index with a router and result merge,
> hybrid (vector + metadata + keyword) search, incremental/batch build, and a
> recall/latency benchmark harness against brute-force ground truth.

Reads: `kinora.md` §8.2 (the episodic / vector store), §8.4 (retrieval policy —
recall under a limited context window), §8.3 (`episodic.search` MCP tool).

## Why this exists (and how it composes, not competes)

The core backend already does vector recall **inside Postgres/pgvector**
(`app/db`) and a *fine* re-rank over those candidates in `app.memory.retrieval`
(MMR, hybrid scoring, budget packing). That is correct for one node. This package
is the **coarse-recall engine that scales out** when the episodic store grows past
a single Postgres — the candidate generator a sharded, in-memory ANN tier would
own, with the same "smaller is closer" ordering so its results drop straight into
the existing re-rank. It edits **nothing** in `app.memory` / `app.search` / the DB;
it is a parallel building block. `app.memory.retrieval` = fine re-rank;
`app.datascale.vectorsearch` = scalable coarse recall.

## Architecture (all under `backend/app/datascale/vectorsearch/`)

```
                         VectorSearchService            (service.py — clean API)
                         ├─ backend select (config.py)
                         ├─ hybrid: dense ⊕ BM25 ⊕ filter (filtering.py)
                         └─ uniform upsert / search / delete / query(Query)
                                       │
        ┌──────────────┬──────────────┼───────────────┬──────────────┐
   HnswIndex      ShardedIndex   QuantizedFlatIndex  BruteForceIndex   builders
   (hnsw.py)      (shard.py)      (pq_index.py)      (brute_force.py)  (builder.py)
   graph ANN      router+merge    PQ/SQ codes +      exact O(n)        batch +
   ins/search/    (merge.py)      ADC + re-rank      ground truth      incremental
   delete+repair  Hash/Attr/Mod                                        + compaction
        └──────────────┴──────────────┴───────────────┴──────────────┘
                                       │
                  distance.py (one ordering convention, NumPy kernels)
                  quantization.py (k-means, ProductQuantizer, ScalarQuantizer)
                  storage.py (mmap-friendly .npy + json segment; serialize)
                  benchmark.py (recall@k / mAP / nDCG / latency vs exact)
                  types.py (Metric, SearchResult, Query, validation)
```

| Module | Responsibility |
|---|---|
| `types.py` | `Metric` (cosine/dot/l2/l2sq, `StrEnum`), `SearchResult`, `Query`, `as_vector`/`as_matrix` validation, the float32 storage dtype. |
| `distance.py` | NumPy distance/similarity kernels; the single "smaller is closer" ordering key (`order_value*`), normalisation, BLAS `pairwise_order`. |
| `hnsw.py` | From-scratch HNSW: multi-layer graph, heuristic neighbour selection, insert / beam-search / delete (tombstone + in-neighbour repair) / `compact()` / `stats()`; tunable `M` / `ef_construction` / `ef`; deterministic RNG. |
| `quantization.py` | `kmeans` (k-means++), `ProductQuantizer` (codebooks + asymmetric distance tables), `ScalarQuantizer` (per-dim int8/16). State (de)serialisation. |
| `pq_index.py` | `QuantizedFlatIndex` — compressed flat index (PQ ADC or SQ decode) with optional exact re-rank of the coarse top candidates; compression-ratio / memory accounting. |
| `filtering.py` | Metadata predicate language (`$eq/$ne/$in/$nin/$gt…/$exists/$contains/$and/$or/$not`); `Bm25KeywordIndex`; `fuse_scores` linear fusion. |
| `merge.py` | K-way merge of sorted per-shard runs (`merge_results`) + an unsorted union/keep-closest variant (`merge_dedup_keep_closest`). |
| `shard.py` | `ShardedIndex` (route-on-write, fan-out-on-query, merge); `HashRouter` / `ModuloRouter` / `AttributeRouter` (the last prunes query shards from a pinned filter); `rebalance_plan` diagnostic; save/load. |
| `builder.py` | `build_hnsw` / `build_sharded` / `build_quantized` (auto-tuned params); `IncrementalBuilder` (streaming inserts + a count/fraction compaction policy). |
| `storage.py` | Persist an index as a segment dir: `vectors.npy` (mmap-able float32) + `meta.json` (graph, maps, tombstones, metadata). `serialize_index`/`deserialize_index` for transport. |
| `benchmark.py` | `exact_ground_truth`, `recall_at_k` / `average_precision` / `ndcg`, `benchmark` (+ latency p50/p95/p99, QPS), `compare`. |
| `config.py` | `VectorSearchConfig` (defaults to the 1152-d cosine Kinora embedding contract); package-local, not wired into global `Settings`. |
| `service.py` | `VectorSearchService` — backend-agnostic facade; pure-ANN `search`, structured `query(Query)` hybrid path, training hook for quantized backends. |

## Key design decisions

- **One ordering convention.** Every backend orders by a "smaller is closer"
  key (`distance.order_value*`): L2 distances pass through; cosine/dot
  similarities are negated. So HNSW heaps, the brute-force baseline, the merge
  and the quantizers all share one comparison, and similarity vs distance metrics
  never branch in the hot path. The metric-native score is returned separately.
- **Cosine normalises on insert.** For the cosine metric vectors are L2-normalised
  on the way in, so a query is a single dot product and PQ's squared-L2 ADC is a
  valid cosine re-rank key (`||q−x||² = 2 − 2·cos` on the unit sphere).
- **HNSW deletes free the external id but tombstone the node.** A delete masks the
  node from results, repairs its live in-neighbours (cross-links them so an
  articulation point can't sever the graph), and **frees the external id** so a
  re-add is a genuine new insert (not infinite `_replace` recursion). Slots are
  reclaimed by `compact()`, which rebuilds from the live set.
- **PQ is a coarse filter, not a final ranker.** Direct PQ recall on isotropic
  data is intentionally modest; the supported recipe is coarse ADC scan → exact
  re-rank of the top candidates (`QuantizedFlatIndex(keep_originals=True)`), which
  recovers near-exact recall at 16× compression. Tests assert both the recipe's
  recall and that the ADC equals decode-then-score.
- **Shard router contract.** Writes route to exactly one shard (an id lives in one
  place — unambiguous delete/update); queries fan out and merge. `AttributeRouter`
  additionally lets a query pinned on its field visit a single shard (a metadata
  filter becomes a shard prune).
- **mmap-friendly persistence.** The bulk bytes (the vector matrix) are a `.npy`
  re-openable with `mmap_mode='r'`; the graph/maps are small JSON. A cold index
  pages its vectors in lazily.
- **Determinism everywhere.** Seeded RNG for level assignment, k-means init and
  shard-seed offsets, so every `recall@k ≥ threshold` assertion is reproducible.

## Additive shared-file changes

**None.** `app/core/config.py` and `app/composition.py` are untouched. The package
ships its own `VectorSearchConfig` (with `from_mapping` to hydrate from any
settings-like object) so a composition root can adopt it without editing the
`Container`. `app/datascale/__init__.py` is a new file (the package root). Nothing
in the existing app imports this package — it is strictly additive and
conflict-free with the ~29 parallel agents.

## Test coverage (deterministic, infra-free; `make lint` clean)

| File | Focus |
|---|---|
| `test_vs_distance.py` | metric semantics, ordering sign, batch vs scalar, validation. |
| `test_vs_brute_force.py` | exactness vs NumPy, swap-remove, prefilter. |
| `test_vs_hnsw.py` | **recall@10 ≥ 0.92 clustered / ≥ 0.85 isotropic / ≥ 0.90 L2 vs brute force**, ef-monotonicity, delete-masking + post-delete recall, compaction invariance, determinism, graph structure. |
| `test_vs_quantization.py` | k-means cluster recovery, SQ near-lossless, PQ recon error + ADC = decode-then-score, state round-trips. |
| `test_vs_pq_index.py` | **PQ+rerank recall ≥ 0.95**, SQ recall ≥ 0.90, compression ratio, filter, delete/update. |
| `test_vs_shard_merge.py` | k-way merge order/dedup, routers, **sharded recall ≥ 0.90 vs brute force**, balance, attribute-router shard prune, persistence-independent compaction. |
| `test_vs_filtering.py` | predicate operators + combinators, BM25 ranking/normalise/remove, fusion. |
| `test_vs_builder.py` | batch builders, auto-params scaling, incremental builder + both compaction policies. |
| `test_vs_storage.py` | save/load round-trip identical, tombstone preservation, mmap on/off, mutable-after-load, bad-version rejection, sharded save/load. |
| `test_vs_benchmark.py` | metric correctness, ground-truth == brute force, brute force = perfect recall, `compare`. |
| `test_vs_service.py` | config validation/hydration, every backend, training gate, metadata filter, hybrid fusion + keyword rescue, delete from both indexes. |

Total: **140 tests**, all green (run split into two groups to fit the CI
wall-clock; HNSW graph build for a few thousand nodes in pure Python is the cost).

## Roadmap (future phases — none required for this milestone)

1. **MCP adapter.** A thin `episodic.search` backend that delegates coarse recall
   to a `VectorSearchService` and hands candidates to `app.memory.retrieval` for
   the §8.4 MMR/budget re-rank — wiring, not new math.
2. **IVF coarse quantizer** (inverted file over k-means cells) in front of PQ for
   sub-linear scan on very large shards (`IVF-PQ`).
3. **Persisted PQ/SQ codebooks + codes** in the segment format (today the file
   format persists HNSW; the quantized index serialises via its quantizer state).
4. **Concurrent shard query** (thread/async fan-out) — the merge already supports
   it; only the fan-out loop needs parallelising.
5. **Filtered HNSW pre-filtering** (entry-point restriction / per-attribute
   subgraphs) for highly selective predicates, beyond today's widened post-filter.
6. **SIMD / Cython hot loop** for `_search_layer` if pure-Python build time ever
   matters in production (tests already cap corpus size to stay fast).
```
