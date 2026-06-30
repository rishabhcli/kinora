# `app.cache.clips` — content-addressed render caching & dedup (DESIGN)

> Avoids paying twice for the same render. Rendering a clip is Kinora's single
> most expensive operation (a hosted Wan / MiniMax video round-trip costs real
> money — kinora.md §11.1). This layer makes an identical render free.

Reads: `kinora.md` §8.7 (caching & dedup), §11.1 (budget / spend), §12.3 (cache
layers — the *request-level dedup* row), §12.5 (per-shot hit/miss observability).

## Why this exists (and how it differs from the two existing caches)

There are now **three** complementary clip-related caches; each owns a distinct
key scope, and this package neither edits nor replaces the other two:

| Cache | Key | Scope | Storage |
|---|---|---|---|
| `app.memory.cache_service` (§8.7) | `shot_hash` = sha1(book+beat+canon_ver+mode+seed+refset) | **book-scoped** idempotency (a re-read never re-renders) | Postgres row (`ShotCacheRepo`) |
| `app.cache` (general) | per-namespace logical keys | any subsystem's memoization | L1 + Redis |
| **`app.cache.clips`** (this) | `RenderKey` = sha256(prompt+camera+seed+mode+ref_digest+provider+model+duration) | **cross-book / cross-session** content reuse | L1 → Redis → object store |

The §8.7 `shot_hash` deliberately folds in `book_id`/`beat_id`, so two *different*
books that request the byte-for-byte same shot compute different hashes and pay
twice. The **content-addressed `RenderKey`** excludes all identity and is derived
purely from the render-determining inputs, so semantically-identical requests
collide **across books and sessions** — the clip is rendered once for the fleet.

## The content key (`keys.py`)

`RenderInputs` is the canonical, render-determining input set (no book/beat). Its
`key()` digests a fully **normalised** canonical form so cosmetic differences
collide:

- prompts: Unicode-NFC → whitespace-collapse → casefold;
- camera: reduced to the meaningful `(move, speed, shot_size)` triple, unknown
  keys dropped, missing keys defaulted (so `{}` ≡ the default block);
- references: order-independent, de-duplicated, whitespace-trimmed SHA-256 digest
  (a shot's identity is the *set* of locked refs, mirroring §8.7);
- duration: quantised to a 0.5s grid (float jitter never splits a clip);
- provider/model/mode/resolution: casefolded.

The key string is `"<version>:<sha256>"`; `version` (`RENDER_KEY_SCHEMA`) lets a
future normalisation change mint fresh keys instead of serving a now-wrong clip.

## Tiers (`store.py`, `tiers.py`)

`build_clip_backend` composes the available tiers fastest-first, reusing the
general package's `MemoryCache` / `RedisCache` / `TieredCache`:

```
L1 MemoryCache  →  L2 RedisCache  →  L3 ObjectStoreCacheBackend
(in-process LRU)   (cross-process)    (durable, fleet-wide)
```

A three-tier stack is `Tiered(L1, Tiered(L2, L3))`; any subset is valid (no Redis →
`Tiered(L1, L3)`; neither → L1-only). The **L3 object tier** persists a tiny JSON
*sidecar* per render key (`clipcache/records/<key>.json`) so a *cold* process that
has never seen a key can still discover the already-rendered clip and serve it for
zero video-seconds. Object stores have no reliable cross-provider per-object TTL,
so the sidecar carries its expiry in-band and is lazily reaped on read; `clear()`
is intentionally a no-op on L3 (the durable tier is the source of truth).

## The facade (`dedup.py` — `RenderCache`)

- **typed get/put/invalidate** keyed by `RenderKey`; a hit charges **0**
  video-seconds and reports `video_seconds_saved`.
- **`get_or_render` — single-flight / request coalescing.** Concurrent callers
  for the same key run the expensive `render` coroutine **once** and share the
  resulting `ClipRecord`; follower cancellation never cancels the leader
  (inherited from `SingleFlight` via the facade). This is the §12.3
  request-level-dedup row made real.
- **cross-book reuse accounting** — each hit records the requesting book on the
  record (re-persisting the widened set with the record's own tags preserved) and
  totals the seconds saved.
- **tag invalidation** — a clip tagged by the entities it depends on
  (`entity:<id>`) is dropped wholesale on a Director edit (the §8.7 cheap-edit
  story, generalised cross-book). Tags are stored *on the record* so a
  reuse-driven re-persist re-applies them rather than stripping them.
- **warmup / prefetch** (`warm`, `prefetch_keys`) — the scheduler can render the
  speculative zone ahead of the cursor (coalescing with any concurrent render) or
  pull keys into the fast tiers.
- **stats** — generic hit/miss/eviction plus `video_seconds_saved` /
  `cross_book_hits`.

Negative caching is **off** for clips (a "not rendered yet" must never be cached
as an absence — that would suppress the render); early-expiry is off (records are
immutable for a content address, so refreshing buys nothing).

## Additive shared-file changes

**One line.** `app/cache/__init__.py` re-exports `RenderCache` / `RenderInputs` /
`RenderKey`. `app/core/config.py` and `app/composition.py` are **untouched**:
`integration.build_render_cache[_from_settings]` reads optional knobs
(`clip_cache_l1_max_entries`, `clip_cache_url_ttl_s`, `clip_cache_record_ttl_s`)
with `getattr` fallbacks, so it works with today's `Settings` and a future one
that adds those fields, with no edit required now. The production
`app.storage.object_store.ObjectStore` satisfies `ClipBlobStore` structurally.

## Tests (all infra-free, deterministic; `make lint` clean)

| File | Focus |
|---|---|
| `tests/test_cache_clips_keys.py` | key stability + every normalisation rule; `from_spec`; cross-book collision |
| `tests/test_cache_clips_store.py` | object-tier round-trip, in-band TTL reap, tag index, fail-open, stack assembly + promotion |
| `tests/test_cache_clips_dedup.py` | get/put/miss, single-flight (one underlying call) + cancellation safety + error propagation, cross-book reuse, TTL (FakeClock), cold-process L3 hit, warmup/prefetch, stats, eviction |

57 tests, all green; the 98 existing `app.cache` tests still pass.
