# `app.cache` — unified multi-tier caching subsystem (DESIGN / roadmap)

> A **general** application cache for the Kinora backend. Distinct from
> `app.memory.cache_service`, which is the shot-hash-specific §8.7 render-dedup
> cache (DB-backed via `ShotCacheRepo`). This package was **not** allowed to edit
> that module; it builds a parallel, reusable layer that any subsystem can adopt.

Reads: `kinora.md` §8.7 (caching & dedup), §12.3 (caching layers), §12.5
(observability — per-shot cache hit/miss).

## Why this exists

§12.3 lists five cache "layers" (shot, keyframe, canon-embedding, reference-video,
request-level dedup). Today only the shot cache exists as a concrete service; the
others were notional. Rather than hand-roll each one, this package provides the
*mechanism* (tiers, TTL, tags, single-flight, negative caching, metrics) so each
logical cache is just a `CacheManager.get("<namespace>", config=...)` away.

## Architecture (all under `backend/app/cache/`)

```
                      ┌───────────────────────────┐
   call sites ───────▶│  Cache  (facade, cache.py) │
   @cached  ─────────▶│  - cache-aside/read/write  │
                      │  - negative caching        │
                      │  - single-flight + XFetch  │
                      │  - tag/key invalidation     │
                      │  - per-namespace metrics    │
                      └─────────────┬──────────────┘
                                    │ stores CacheEntry by qualified key
                      ┌─────────────▼──────────────┐
                      │     CacheBackend (ABC)      │
                      └───┬───────────┬─────────┬───┘
            MemoryCache (L1)   RedisCache (L2)   TieredCache (L1→L2)   NullCache
            LRU+TTL+tags       envelope+TTL+tag  promote/write-through  no-op
                               sets, binary-safe
```

| Module | Responsibility |
|---|---|
| `clock.py` | `Clock` protocol, `SystemClock`, **`FakeClock`** (deterministic test harness). |
| `errors.py` | `CacheError` family (`CacheBackendError`, `SerializationError`, `SingleFlightError`). |
| `codecs.py` | `Codec` protocol + `JsonCodec` / `PickleCodec` / `BytesCodec`. |
| `entry.py` | Immutable `CacheEntry` (value, TTL/expiry, tags, negative marker, XFetch early-expiry math). |
| `metrics.py` | `CacheMetrics` — thread-safe per-namespace hit/miss/evict/load/… counters + `NamespaceStats`. |
| `interface.py` | `CacheBackend` ABC (get/set/delete/clear/delete_tag/health + batch defaults). |
| `memory.py` | L1: `MemoryCache` — `OrderedDict` LRU, lazy TTL purge, reverse tag index. |
| `redis_backend.py` | L2: `RedisCache` — one string per entry (versioned envelope), native PX TTL, tag sets, prefix scoping. |
| `tiered.py` | `TieredCache` — L1 in front of L2, promotion on L2 hit, write-through, L2 fail-open. |
| `null.py` | `NullCache` — caching disabled. |
| `singleflight.py` | `SingleFlight` — collapse concurrent loads of one key into one execution. |
| `keys.py` | `qualify`, `fingerprint`, `derive_key` (stable arg-hash for `@cached`). |
| `cache.py` | `Cache` facade + `CacheConfig` + `ABSENT` sentinel + `tag_key_for`. |
| `decorator.py` | `@cached` — memoize async functions; `.cache_key()` / `.invalidate()` helpers. |
| `factory.py` | `memory_cache` / `redis_cache` / `tiered_cache` / `null_cache` + `CacheManager`. |
| `integration.py` | `build_cache_manager[_from_settings]`, `binary_redis_from_url`, `redis_lock_factory`. |

## Key design decisions

- **Injected clock everywhere.** No TTL/early-expiry path reads wall time directly;
  tests drive a `FakeClock`, so expiry is deterministic with no `sleep`.
- **In-memory-only mode is first-class.** `CacheManager()` with no `redis=` is a
  pure L1 manager that needs no infra — the default for tests and Redis-less envs.
- **Fail-open by default.** A Redis blip degrades the tiered cache to L1-only (and
  the facade to a soft miss) rather than taking the request down — the §12.4 "the
  film never hard-stops" ethos applied to the cache. Toggle via
  `CacheConfig.fail_open` / `TieredCache(l2_fail_open=...)`.
- **Stampede protection is layered:** in-process `SingleFlight` (always) +
  optional cross-process Redis lock (`lock_factory`) + probabilistic early expiry
  (XFetch) so the population refreshes a hot key gradually instead of all at once.
- **Negative caching** with a short TTL keeps "absent" lookups cheap (§8.5 timely
  removal); a loader returning the miss sentinel is recorded as a negative entry.
- **Tags generalise the §8.7 cheap-edit story:** tag a cached value by the entity
  it depends on; changing that entity drops only that tag. Tags are namespaced so
  two caches' `"hot"` tags are independent.
- **Binary-safe L2 envelope:** `version | header_len | header_json | payload`,
  with a native Redis `PX` TTL so Redis reaps expirations itself.

## Additive shared-file changes

**None.** `app/core/config.py` and `app/composition.py` are untouched. Wiring is
provided *inside* this package (`integration.py`): a composition root can call
`build_cache_manager_from_settings(settings, lock_redis=container.redis)` and
store the result without modifying the `Container` dataclass. This keeps the
package strictly additive and conflict-free with other agents.

## Test coverage (all green; `make lint` clean across the backend)

| File | Focus | Infra |
|---|---|---|
| `tests/test_cache_primitives.py` | clock, codecs, entry, keys, metrics | none |
| `tests/test_cache_memory_backend.py` | LRU eviction, TTL purge, tags, batch | none |
| `tests/test_cache_facade.py` | cache-aside/read/write, negatives, invalidation, fail-open | none |
| `tests/test_cache_stampede.py` | single-flight, early expiry, cross-process lock | none |
| `tests/test_cache_decorator.py` | `@cached` key derivation, tags, helpers | none |
| `tests/test_cache_tiered.py` | promotion, write-through, fan-out, factories, `CacheManager` | none |
| `tests/test_cache_redis.py` | envelope round-trips (none) + live L2 (gated on `KINORA_TEST_REDIS_URL`, db 15) | redis |
| `tests/test_cache_integration.py` | composition helpers + public surface | none |

Total: **103 cache tests** (98 infra-free + 5 live-Redis when `KINORA_TEST_REDIS_URL`
is set; verified against `redis://localhost:6379/15`).

## Roadmap (future phases — none required for this milestone)

1. **Adopters.** Wire concrete §12.3 layers on top of `CacheManager`:
   canon-embedding cache (`embed:{entity}:{version}`, tag `entity:{id}`), keyframe
   cache, retrieval/query cache for `MemoryTools`. Each is a thin `@cached` wrapper.
2. **Metrics bridge.** Optional adapter forwarding `CacheMetrics.snapshot()` into
   the Prometheus registry owned by `app.observability.metrics` (read-only; that
   surface is another package's lane, so this stays a pull, not an edit).
3. **Redis cluster / pipeline batching.** `get_many`/`set_many` overrides on
   `RedisCache` using `MGET`/pipelined `SET` for hot batch paths.
4. **TTL jitter on write** (in addition to XFetch on read) to further desynchronise
   bulk-populated namespaces.
5. **`scan`-based namespace size introspection** for the L2 tier (dashboards).
6. **Compression codec** (zstd/gzip envelope flag) for large cached payloads.
```
