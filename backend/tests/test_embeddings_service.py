"""EmbeddingStore facade: cached embeds, identity wiring, migrate, compact."""

from __future__ import annotations

from app.embeddings.config import EmbeddingStoreSettings
from app.embeddings.embedder import FakeEmbedder
from app.embeddings.models import EntityKind
from app.embeddings.service import EmbeddingStore
from app.embeddings.vectors import VectorSpace


def make_store(**overrides: object) -> EmbeddingStore:
    cfg = EmbeddingStoreSettings(model="m", dimension=16, **overrides)
    return EmbeddingStore.in_memory_fake(settings=cfg, seed=2)


async def test_embed_image_is_cached() -> None:
    store = make_store()
    v1 = await store.embed_image(b"frame")
    v2 = await store.embed_image(b"frame")
    assert v1.values == v2.values
    assert store.cache.stats.hits == 1
    assert store.cache.stats.misses == 1


async def test_embed_text_cache_can_be_bypassed() -> None:
    store = make_store()
    await store.embed_text("hello", use_cache=False)
    await store.embed_text("hello", use_cache=False)
    assert store.cache.stats.lookups == 0  # never consulted the cache


async def test_identity_roundtrip_through_facade() -> None:
    store = make_store()
    [ref_vec] = await store.embedder.embed_images([b"elsa"])
    await store.identity.add_reference(
        ref_id="r1",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=ref_vec,
        enforce_admission=False,
    )
    verdict = await store.identity.verify(
        book_id="book1", entity_key="char_elsa", frame_bytes=b"elsa"
    )
    assert verdict.is_match


async def test_migrate_to_reembeds_cache_and_swaps_active() -> None:
    store = make_store()
    # Populate the cache under the v1 space.
    await store.embed_text("elsa")
    old_space = store.space

    sources = {"text:" + __import__("hashlib").sha256(b"text\x00elsa").hexdigest(): "elsa"}

    async def resolve(key: str) -> bytes | str | None:
        return sources.get(key)

    new_space = VectorSpace(
        provider=old_space.provider,
        model=old_space.model,
        dimension=old_space.dimension,
        version=old_space.version + 1,
    )
    new_embedder = FakeEmbedder(new_space, seed=2)
    report = await store.migrate_to(new_embedder, resolve_source=resolve)
    assert report.reembedded == 1
    # Active embedder + identity store now use the new space.
    assert store.space == new_space
    v = await store.embed_text("elsa")
    assert v.space == new_space


async def test_compact_through_facade() -> None:
    store = make_store()
    [ref_vec] = await store.embedder.embed_images([b"elsa"])
    await store.identity.add_reference(
        ref_id="r1",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=ref_vec,
        version=1,
        enforce_admission=False,
    )
    await store.identity.add_reference(
        ref_id="r2",
        entity_key="char_elsa",
        book_id="book1",
        kind=EntityKind.CHARACTER,
        vector=ref_vec,  # exact duplicate
        version=2,
        enforce_admission=False,
    )
    report = await store.compact(dedup_threshold=0.99)
    assert report.deduped == 1
    refs = await store.identity.list_references(book_id="book1", entity_key="char_elsa")
    assert len(refs) == 1


async def test_from_settings_inherits_model_and_dimension() -> None:
    class _AppSettings:
        embed_model_image = "tongyi-embedding-vision-plus"
        embed_dim = 1152

    cfg = EmbeddingStoreSettings.from_settings(_AppSettings())
    assert cfg.model == "tongyi-embedding-vision-plus"
    assert cfg.dimension == 1152


async def test_settings_reject_threshold_must_not_exceed_match() -> None:
    import pytest

    with pytest.raises(ValueError):
        EmbeddingStoreSettings(match_threshold=0.5, reject_threshold=0.9)
