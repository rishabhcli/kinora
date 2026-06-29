"""Versioning: immutability, content addressing, lineage DAG, resolve/tag."""

from __future__ import annotations

import pytest

from app.mlplatform.datasets.contracts import AgentRole, Dataset, TaskType, TraceExample
from app.mlplatform.datasets.errors import ImmutabilityError, VersionError
from app.mlplatform.datasets.versioning import (
    DatasetRegistry,
    InMemoryVersionStore,
    Operation,
    make_version_id,
)


def _ds(name: str, ids: list[str]) -> Dataset:
    exs = [
        TraceExample(
            id=i,
            role=AgentRole.ADAPTER,
            task=TaskType.SFT,
            prompt_key="adapter@v3",
            prompt_version="3.0.0",
            model="qwen-plus",
            input={"page_text": i},
            output="o",
            book_id="bk0",
        )
        for i in ids
    ]
    return Dataset.from_examples(name, exs)


def test_commit_is_content_addressed_and_idempotent() -> None:
    reg = DatasetRegistry()
    ds = _ds("d", ["a", "b"])
    v1 = reg.commit(ds, operation=Operation.INGEST)
    v2 = reg.commit(ds, operation=Operation.INGEST)  # identical → same version
    assert v1.version_id == v2.version_id
    assert len(reg.history("d")) == 1


def test_different_content_distinct_versions() -> None:
    reg = DatasetRegistry()
    v1 = reg.commit(_ds("d", ["a"]), operation=Operation.INGEST)
    v2 = reg.commit(_ds("d", ["a", "b"]), operation=Operation.DEDUP, parents=[v1.version_id])
    assert v1.version_id != v2.version_id
    assert reg.latest("d").version_id == v2.version_id


def test_immutability_overwrite_rejected() -> None:
    store = InMemoryVersionStore()
    reg = DatasetRegistry(store=store)
    v = reg.commit(_ds("d", ["a"]), operation=Operation.INGEST)
    # Forge a different-content version sharing the id → must be rejected.
    from dataclasses import replace

    forged = replace(v, content_hash="different", dataset=_ds("d", ["x"]))
    with pytest.raises(ImmutabilityError):
        store.put(forged)


def test_lineage_walk_is_topological() -> None:
    reg = DatasetRegistry()
    v1 = reg.commit(_ds("d", ["a"]), operation=Operation.INGEST)
    v2 = reg.commit(_ds("d", ["a", "b"]), operation=Operation.SCRUB, parents=[v1.version_id])
    v3 = reg.commit(
        _ds("d", ["a", "b", "c"]), operation=Operation.SPLIT, parents=[v2.version_id]
    )
    walk = reg.lineage(v3.version_id)
    ids = [n.version_id for n in walk]
    assert ids.index(v1.version_id) < ids.index(v2.version_id) < ids.index(v3.version_id)
    assert walk[0].operation == "ingest"


def test_merge_two_parents() -> None:
    reg = DatasetRegistry()
    a = reg.commit(_ds("a", ["a1"]), operation=Operation.INGEST)
    b = reg.commit(_ds("b", ["b1"]), operation=Operation.INGEST)
    merged = reg.commit(
        _ds("merged", ["a1", "b1"]),
        operation=Operation.MERGE,
        parents=[a.version_id, b.version_id],
    )
    walk = reg.lineage(merged.version_id)
    assert {n.version_id for n in walk} == {a.version_id, b.version_id, merged.version_id}


def test_unknown_parent_rejected() -> None:
    reg = DatasetRegistry()
    with pytest.raises(VersionError):
        reg.commit(_ds("d", ["a"]), operation=Operation.DEDUP, parents=["nope"])


def test_resolve_by_id_name_tag() -> None:
    reg = DatasetRegistry()
    v = reg.commit(_ds("d", ["a"]), operation=Operation.INGEST)
    reg.tag(v.version_id, "rm_v1")
    assert reg.resolve(v.version_id).version_id == v.version_id
    assert reg.resolve("d").version_id == v.version_id
    assert reg.resolve("rm_v1").version_id == v.version_id
    assert reg.resolve(f"d@{v.version_id}").version_id == v.version_id


def test_latest_unknown_name_raises() -> None:
    with pytest.raises(VersionError):
        DatasetRegistry().latest("ghost")


def test_make_version_id_stable() -> None:
    a = make_version_id("d", "hash1", ["p1"])
    b = make_version_id("d", "hash1", ["p1"])
    c = make_version_id("d", "hash1", ["p2"])
    assert a == b
    assert a != c
