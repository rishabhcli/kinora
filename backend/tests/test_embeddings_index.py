"""In-memory vector index: k-NN, metadata filters, namespaces, space guarding."""

from __future__ import annotations

import pytest

from app.embeddings.index import (
    InMemoryVectorIndex,
    MetadataFilter,
    VectorRecord,
)
from app.embeddings.vectors import EmbeddingVector, SpaceMismatch, VectorSpace

SPACE = VectorSpace(provider="p", model="m", dimension=4, version=1)
OTHER_SPACE = VectorSpace(provider="p", model="m", dimension=4, version=2)


def vec(values: list[float]) -> EmbeddingVector:
    return EmbeddingVector.create(SPACE, values)


def rec(rid: str, values: list[float], ns: str = "_default", **md: object) -> VectorRecord:
    return VectorRecord(id=rid, vector=vec(values), namespace=ns, metadata=md)


async def seeded_index() -> InMemoryVectorIndex:
    idx = InMemoryVectorIndex()
    await idx.upsert(
        [
            rec("x+", [1.0, 0.0, 0.0, 0.0], kind="character", version=1),
            rec("y+", [0.0, 1.0, 0.0, 0.0], kind="location", version=2),
            rec("z+", [0.0, 0.0, 1.0, 0.0], kind="character", version=3),
            rec("xy", [1.0, 1.0, 0.0, 0.0], kind="character", version=1),
        ]
    )
    return idx


async def test_knn_orders_by_cosine() -> None:
    idx = await seeded_index()
    hits = await idx.search(vec([1.0, 0.0, 0.0, 0.0]), top_k=4)
    assert hits[0].record.id == "x+"
    assert hits[0].score == pytest.approx(1.0)
    # xy at 45° to x-axis comes before the orthogonal y+/z+.
    assert hits[1].record.id == "xy"
    assert hits[1].score == pytest.approx(0.70710678, abs=1e-6)


async def test_knn_respects_top_k() -> None:
    idx = await seeded_index()
    hits = await idx.search(vec([1.0, 0.0, 0.0, 0.0]), top_k=2)
    assert len(hits) == 2
    assert [h.record.id for h in hits] == ["x+", "xy"]


async def test_top_k_zero_returns_empty() -> None:
    idx = await seeded_index()
    assert await idx.search(vec([1.0, 0.0, 0.0, 0.0]), top_k=0) == []


async def test_metadata_filter_eq_and_in() -> None:
    idx = await seeded_index()
    chars = await idx.search(
        vec([1.0, 1.0, 1.0, 0.0]),
        top_k=10,
        filter=MetadataFilter().eq("kind", "character"),
    )
    assert {h.record.id for h in chars} == {"x+", "z+", "xy"}

    v123 = await idx.search(
        vec([1.0, 1.0, 1.0, 0.0]),
        top_k=10,
        filter=MetadataFilter().in_("version", [2, 3]),
    )
    assert {h.record.id for h in v123} == {"y+", "z+"}


async def test_metadata_filter_range_and_exists() -> None:
    idx = await seeded_index()
    hi = await idx.search(
        vec([0.0, 0.0, 1.0, 0.0]),
        top_k=10,
        filter=MetadataFilter().gte("version", 2),
    )
    assert {h.record.id for h in hi} == {"y+", "z+"}

    none = await idx.search(
        vec([1.0, 0.0, 0.0, 0.0]),
        top_k=10,
        filter=MetadataFilter().exists("missing_key"),
    )
    assert none == []


async def test_metadata_filter_contains_list_field() -> None:
    idx = InMemoryVectorIndex()
    await idx.upsert(
        [
            rec("front", [1.0, 0.0, 0.0, 0.0], poses=["front", "wide"]),
            rec("prof", [0.0, 1.0, 0.0, 0.0], poses=["profile"]),
        ]
    )
    hits = await idx.search(
        vec([1.0, 1.0, 0.0, 0.0]),
        top_k=10,
        filter=MetadataFilter().contains("poses", "front"),
    )
    assert {h.record.id for h in hits} == {"front"}


async def test_namespace_isolation() -> None:
    idx = InMemoryVectorIndex()
    await idx.upsert([rec("a", [1.0, 0.0, 0.0, 0.0], ns="book1:char_elsa")])
    await idx.upsert([rec("b", [1.0, 0.0, 0.0, 0.0], ns="book2:char_elsa")])
    h1 = await idx.search(vec([1.0, 0.0, 0.0, 0.0]), top_k=10, namespace="book1:char_elsa")
    assert {h.record.id for h in h1} == {"a"}
    h2 = await idx.search(vec([1.0, 0.0, 0.0, 0.0]), top_k=10, namespace="book2:char_elsa")
    assert {h.record.id for h in h2} == {"b"}
    assert await idx.count() == 2
    assert sorted(await idx.namespaces()) == ["book1:char_elsa", "book2:char_elsa"]


async def test_upsert_is_idempotent_on_id() -> None:
    idx = InMemoryVectorIndex()
    await idx.upsert([rec("a", [1.0, 0.0, 0.0, 0.0], tag="old")])
    await idx.upsert([rec("a", [0.0, 1.0, 0.0, 0.0], tag="new")])
    assert await idx.count() == 1
    got = await idx.get("a")
    assert got is not None and got.metadata["tag"] == "new"


async def test_delete_and_drop_namespace() -> None:
    idx = await seeded_index()
    assert await idx.delete(["x+", "nope"]) == 1
    assert await idx.count() == 3
    dropped = await idx.drop_namespace("_default")
    assert dropped == 3
    assert await idx.count() == 0


async def test_search_skips_mismatched_space_records() -> None:
    # An unpinned index permits mixed spaces in storage but never scores across.
    idx = InMemoryVectorIndex()
    await idx.upsert([rec("a", [1.0, 0.0, 0.0, 0.0])])
    await idx.upsert(
        [VectorRecord(id="b", vector=EmbeddingVector.create(OTHER_SPACE, [1.0, 0.0, 0.0, 0.0]))]
    )
    hits = await idx.search(vec([1.0, 0.0, 0.0, 0.0]), top_k=10)
    assert {h.record.id for h in hits} == {"a"}  # 'b' is in a different space


async def test_pinned_index_rejects_foreign_space() -> None:
    idx = InMemoryVectorIndex(expected_space=SPACE)
    with pytest.raises(SpaceMismatch):
        await idx.upsert(
            [VectorRecord(id="b", vector=EmbeddingVector.create(OTHER_SPACE, [1.0, 0, 0, 0]))]
        )
    with pytest.raises(SpaceMismatch):
        await idx.search(EmbeddingVector.create(OTHER_SPACE, [1.0, 0, 0, 0]), top_k=1)
