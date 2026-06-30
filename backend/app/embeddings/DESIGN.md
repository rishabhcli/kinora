# `app/embeddings` — provider-agnostic embeddings / identity vector store (v2)

The durable, queryable **store** beneath Round-1's cross-provider identity
conditioning (kinora.md §8 canon / identity-lock). Round-1's
`app.providers.embeddings.EmbeddingProvider` only *produces* vectors from one
hosted DashScope model; this subsystem makes vectors a first-class, versioned,
queryable asset that the render/Critic loop can store, search, and verify
against — without ever silently mixing vectors from different embedders.

Everything here is **additive** and lives under this namespace. No file outside
`app/embeddings/` is touched; nothing flips `KINORA_LIVE_VIDEO`; tests need no
infra and no network.

## Modules

| Module | What it owns |
|---|---|
| `vectors.py` | `EmbeddingVector` (immutable, normalized) + `VectorSpace` (`provider:model:dN:vM`). All arithmetic refuses to cross spaces (`SpaceMismatch`) or dimensions (`DimensionMismatch`). |
| `embedder.py` | `Embedder` protocol + `DashScopeEmbedder` (wraps round-1), `OpenAIEmbedder`, `LocalClipEmbedder`, and a deterministic seeded `FakeEmbedder` for tests. `BaseEmbedder.embed()` handles mixed image/text batches in order. |
| `index.py` | `VectorIndex` protocol + exact `InMemoryVectorIndex`: upsert / k-NN / `MetadataFilter` / namespaces / space guard. Shaped so a pgvector/HNSW backend drops in unchanged. |
| `identity.py` | `IdentityStore`: versioned reference images + descriptors per entity; `verify()` → `MATCH`/`UNCERTAIN`/`REJECT`; `best_reference()` by pose/query/version; per-`{book}:{entity}` namespace isolation; admission control against drift. |
| `cache.py` | `InMemoryEmbeddingCache` (content+space addressed, LRU) + `reembed_stale()` re-embed-on-model-change migration. |
| `maintenance.py` | `compact_index()`: dedup near-duplicates, version-prune, orphan-sweep. Idempotent. |
| `service.py` | `EmbeddingStore` facade wiring embedder + cache + index + identity; `in_memory_fake()` (tests) / `from_dashscope()` (prod). |
| `config.py` | `EmbeddingStoreSettings` (additive; inherits model id + dimension from `app.core.config.Settings` via `from_settings`). |

## Key invariants

- **No cross-space comparison.** A vector carries its `VectorSpace`; cosine/dot
  raise `SpaceMismatch` across spaces, the index skips foreign-space records on
  search, and the cache keys by content **and** space. Swapping the model bumps
  `version`, which invalidates old cache/index entries by identity alone.
- **Namespace isolation.** Identity references live under `{book_id}:{entity_key}`,
  so two characters — or two books — never bleed into each other's matches.
- **Verdict thresholds.** `match_threshold` / `reject_threshold` (cosine on unit
  vectors) bracket the `MATCH` / `UNCERTAIN` / `REJECT` decision — the storeful
  form of the round-1 character-consistency score (CCS).
- **Re-embed migration is non-destructive + re-runnable.** New-space vectors are
  written alongside old ones; a separate `invalidate_space` can drop the old set.

## Wiring (not done here — left to the composition root)

`EmbeddingStore.from_dashscope(provider=..., app_settings=...)` builds the
production store lazily from the existing `EmbeddingProvider` + `Settings`, so
importing this package never touches the network (matching the backend's lazy-DI
convention). A pgvector-backed `VectorIndex` over the existing
`entities.embedding` column is the natural production index and satisfies the
same protocol.
