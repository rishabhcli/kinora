"""Embedding cache: content+space addressing, LRU eviction, re-embed migration."""

from __future__ import annotations

from app.embeddings.cache import (
    InMemoryEmbeddingCache,
    content_key_for_image,
    content_key_for_text,
    reembed_stale,
)
from app.embeddings.embedder import FakeEmbedder
from app.embeddings.vectors import EmbeddingVector, VectorSpace

SPACE_V1 = VectorSpace(provider="p", model="m", dimension=8, version=1)
SPACE_V2 = SPACE_V1.bumped()


async def test_cache_hit_and_miss() -> None:
    cache = InMemoryEmbeddingCache()
    e = FakeEmbedder(SPACE_V1, seed=3)
    key = content_key_for_text("elsa")
    assert await cache.get(key, SPACE_V1) is None
    assert cache.stats.misses == 1

    [v] = await e.embed_texts(["elsa"])
    await cache.put(key, v)
    hit = await cache.get(key, SPACE_V1)
    assert hit is not None and hit.values == v.values
    assert cache.stats.hits == 1


async def test_cache_is_space_scoped() -> None:
    cache = InMemoryEmbeddingCache()
    key = content_key_for_text("elsa")
    v1 = EmbeddingVector.create(SPACE_V1, [1.0] + [0.0] * 7)
    await cache.put(key, v1)
    # Same content key but a different space is a MISS (no silent cross-model reuse).
    assert await cache.get(key, SPACE_V2) is None
    assert await cache.get(key, SPACE_V1) is not None
    # Both spaces can coexist under one content key.
    v2 = EmbeddingVector.create(SPACE_V2, [0.0, 1.0] + [0.0] * 6)
    await cache.put(key, v2)
    spaces = {s.key for s in await cache.spaces_for(key)}
    assert spaces == {SPACE_V1.key, SPACE_V2.key}


async def test_lru_eviction() -> None:
    cache = InMemoryEmbeddingCache(max_entries=2)
    for i in range(3):
        k = content_key_for_text(f"t{i}")
        await cache.put(k, EmbeddingVector.create(SPACE_V1, [float(i)] + [0.0] * 7))
    # t0 (LRU) evicted; t1, t2 remain.
    keys = await cache.keys()
    assert content_key_for_text("t0") not in keys
    assert len(keys) == 2
    assert cache.stats.evictions == 1


async def test_lru_get_marks_mru() -> None:
    cache = InMemoryEmbeddingCache(max_entries=2)
    k0, k1, k2 = (content_key_for_text(f"t{i}") for i in range(3))
    await cache.put(k0, EmbeddingVector.create(SPACE_V1, [1.0] + [0.0] * 7))
    await cache.put(k1, EmbeddingVector.create(SPACE_V1, [2.0] + [0.0] * 7))
    await cache.get(k0, SPACE_V1)  # touch k0 -> now MRU
    await cache.put(k2, EmbeddingVector.create(SPACE_V1, [3.0] + [0.0] * 7))
    keys = await cache.keys()
    assert k1 not in keys  # k1 was LRU and got evicted, not k0
    assert k0 in keys and k2 in keys


async def test_invalidate_space() -> None:
    cache = InMemoryEmbeddingCache()
    key = content_key_for_text("x")
    await cache.put(key, EmbeddingVector.create(SPACE_V1, [1.0] + [0.0] * 7))
    await cache.put(key, EmbeddingVector.create(SPACE_V2, [1.0] + [0.0] * 7))
    removed = await cache.invalidate_space(SPACE_V1)
    assert removed == 1
    assert {s.key for s in await cache.spaces_for(key)} == {SPACE_V2.key}


async def test_reembed_migration_recomputes_stale_entries() -> None:
    # Cache holds v1 vectors for two contents; migrate to a v2 embedder.
    cache = InMemoryEmbeddingCache()
    old = FakeEmbedder(SPACE_V1, seed=1)
    new = FakeEmbedder(SPACE_V2, seed=1)

    sources: dict[str, bytes | str] = {
        content_key_for_text("elsa"): "elsa",
        content_key_for_image(b"frame"): b"frame",
    }
    for k, payload in sources.items():
        if isinstance(payload, bytes):
            [v] = await old.embed_images([payload])
        else:
            [v] = await old.embed_texts([payload])
        await cache.put(k, v)

    async def resolve(key: str) -> bytes | str | None:
        return sources.get(key)

    report = await reembed_stale(cache, target=new, resolve_source=resolve)
    assert report.examined == 2
    assert report.reembedded == 2
    assert report.failed == 0
    assert report.changed
    # Every content now has a v2 vector that matches a direct v2 embed.
    [expected_text] = await new.embed_texts(["elsa"])
    got = await cache.get(content_key_for_text("elsa"), SPACE_V2)
    assert got is not None and got.values == expected_text.values


async def test_reembed_skips_current_and_counts_failures() -> None:
    cache = InMemoryEmbeddingCache()
    new = FakeEmbedder(SPACE_V2, seed=1)
    # One entry already in the target space (skip), one with no source (fail).
    already = content_key_for_text("already")
    [cur] = await new.embed_texts(["already"])
    await cache.put(already, cur)
    orphan = content_key_for_text("orphan")
    await cache.put(orphan, EmbeddingVector.create(SPACE_V1, [1.0] + [0.0] * 7))

    async def resolve(key: str) -> bytes | str | None:
        return "already" if key == already else None

    report = await reembed_stale(cache, target=new, resolve_source=resolve)
    assert report.skipped_current == 1
    assert report.failed == 1
    assert orphan in report.failed_ids
    assert report.reembedded == 0
