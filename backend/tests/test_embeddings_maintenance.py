"""Maintenance: dedup, version pruning, orphan sweep, idempotence."""

from __future__ import annotations

from app.embeddings.index import InMemoryVectorIndex, VectorRecord
from app.embeddings.maintenance import compact_index
from app.embeddings.vectors import EmbeddingVector, VectorSpace

SPACE = VectorSpace(provider="p", model="m", dimension=4, version=1)


def vec(values: list[float]) -> EmbeddingVector:
    return EmbeddingVector.create(SPACE, values)


def rec(rid: str, values: list[float], ns: str, **md: object) -> VectorRecord:
    return VectorRecord(id=rid, vector=vec(values), namespace=ns, metadata=md)


async def test_dedup_near_duplicates_keeps_preferred() -> None:
    idx = InMemoryVectorIndex()
    ns = "book1:char_elsa"
    await idx.upsert(
        [
            rec("a", [1.0, 0.0, 0.0, 0.0], ns, entity_key="e", version=1, created_at=1.0),
            # 'b' is an almost-identical duplicate of 'a' but a newer version -> 'b' kept.
            rec("b", [0.999, 0.001, 0.0, 0.0], ns, entity_key="e", version=2, created_at=2.0),
            rec("c", [0.0, 1.0, 0.0, 0.0], ns, entity_key="e", version=1, created_at=1.0),
        ]
    )
    report = await compact_index(idx, dedup_threshold=0.99)
    assert report.deduped == 1
    remaining = {r.id for r in await idx.iter_records(namespace=ns)}
    assert remaining == {"b", "c"}  # 'a' dropped (dup of higher-version 'b')


async def test_version_pruning_keeps_newest_n() -> None:
    idx = InMemoryVectorIndex()
    ns = "book1:char_elsa"
    await idx.upsert(
        [
            rec("v1", [1.0, 0.0, 0.0, 0.0], ns, entity_key="char_elsa", version=1),
            rec("v2", [0.0, 1.0, 0.0, 0.0], ns, entity_key="char_elsa", version=2),
            rec("v3", [0.0, 0.0, 1.0, 0.0], ns, entity_key="char_elsa", version=3),
        ]
    )
    report = await compact_index(idx, keep_versions=2, dedup_threshold=1.1)
    assert report.version_pruned == 1
    remaining = {r.id for r in await idx.iter_records(namespace=ns)}
    assert remaining == {"v2", "v3"}


async def test_orphan_namespace_swept() -> None:
    idx = InMemoryVectorIndex()
    ns = "book1:char_elsa"
    await idx.upsert(
        [
            rec("a", [1.0, 0.0, 0.0, 0.0], ns, entity_key="char_elsa", version=1, created_at=1.0),
            rec("b", [1.0, 0.0, 0.0, 0.0], ns, entity_key="char_elsa", version=1, created_at=2.0),
        ]
    )
    # Both identical -> one removed; namespace still non-empty (1 left), not orphaned.
    report = await compact_index(idx, dedup_threshold=0.99)
    assert report.deduped == 1
    assert await idx.count(namespace=ns) == 1


async def test_idempotent_second_pass_removes_nothing() -> None:
    idx = InMemoryVectorIndex()
    ns = "book1:char_elsa"
    await idx.upsert(
        [
            rec("a", [1.0, 0.0, 0.0, 0.0], ns, entity_key="e", version=1, created_at=1.0),
            rec("b", [0.999, 0.001, 0.0, 0.0], ns, entity_key="e", version=2, created_at=2.0),
        ]
    )
    first = await compact_index(idx, dedup_threshold=0.99)
    assert first.deduped == 1
    second = await compact_index(idx, dedup_threshold=0.99)
    assert second.total_removed == 0


async def test_compaction_is_namespace_scoped() -> None:
    idx = InMemoryVectorIndex()
    await idx.upsert(
        [
            rec("a1", [1.0, 0.0, 0.0, 0.0], "ns_a", entity_key="e", version=1, created_at=1.0),
            rec("a2", [1.0, 0.0, 0.0, 0.0], "ns_a", entity_key="e", version=1, created_at=2.0),
            rec("b1", [1.0, 0.0, 0.0, 0.0], "ns_b", entity_key="e", version=1, created_at=1.0),
            rec("b2", [1.0, 0.0, 0.0, 0.0], "ns_b", entity_key="e", version=1, created_at=2.0),
        ]
    )
    report = await compact_index(idx, namespaces=["ns_a"], dedup_threshold=0.99)
    assert report.deduped == 1
    assert await idx.count(namespace="ns_a") == 1
    assert await idx.count(namespace="ns_b") == 2  # untouched
