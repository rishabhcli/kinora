"""Contracts: immutability, content addressing, and the Dataset algebra."""

from __future__ import annotations

import pytest

from app.mlplatform.datasets.contracts import (
    AgentRole,
    Dataset,
    DirectorEdit,
    QAVerdict,
    Split,
    TaskType,
    TraceExample,
    canonical_json,
    stable_hash,
)
from app.mlplatform.datasets.errors import DatasetError


def _ex(ex_id: str = "ex_1", **kw: object) -> TraceExample:
    defaults: dict[str, object] = {
        "id": ex_id,
        "role": AgentRole.ADAPTER,
        "task": TaskType.SFT,
        "prompt_key": "adapter@v3",
        "prompt_version": "3.0.0",
        "model": "qwen-plus",
        "input": {"page_text": "hello"},
        "output": "world",
    }
    defaults.update(kw)
    return TraceExample(**defaults)  # type: ignore[arg-type]


def test_canonical_json_is_order_invariant() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert stable_hash({"a": 1}) == stable_hash({"a": 1})


def test_content_hash_ignores_provenance() -> None:
    a = _ex("ex_a", trace_id="t1", session_id="s1", book_id="bk1")
    b = _ex("ex_b", trace_id="t2", session_id="s2", book_id="bk1")
    # Same semantic payload, different ids/provenance → same content hash.
    assert a.content_hash == b.content_hash


def test_content_hash_changes_with_payload() -> None:
    a = _ex(output="one")
    b = _ex(output="two")
    assert a.content_hash != b.content_hash


def test_content_hash_includes_qa_and_edits() -> None:
    base = _ex()
    with_qa = _ex(qa=QAVerdict(passed=True, score=0.9))
    with_edit = _ex(director_edits=(DirectorEdit(instruction="fix"),))
    assert base.content_hash != with_qa.content_hash != with_edit.content_hash


def test_group_key_defaults_to_book_then_id() -> None:
    assert _ex(book_id="bk9").group_key == "bk9"
    assert _ex("ex_lonely", book_id=None).group_key == "ex_lonely"


def test_immutable_updates_return_new_objects() -> None:
    ex = _ex()
    labeled = ex.with_labels({"quality": "good"})
    assert ex.labels == {}
    assert labeled.labels == {"quality": "good"}
    split = labeled.with_split(Split.TEST)
    assert labeled.split is Split.UNASSIGNED
    assert split.split is Split.TEST


def test_empty_id_rejected() -> None:
    with pytest.raises(DatasetError):
        _ex("")


def test_dataset_rejects_duplicate_ids() -> None:
    with pytest.raises(DatasetError):
        Dataset(name="d", examples=(_ex("dup"), _ex("dup")))


def test_dataset_algebra_filter_map_concat() -> None:
    ds = Dataset.from_examples(
        "d", [_ex("a", role=AgentRole.ADAPTER), _ex("b", role=AgentRole.CRITIC)]
    )
    assert len(ds.by_role(AgentRole.ADAPTER)) == 1
    relabeled = ds.map(lambda e: e.with_labels({"quality": "good"}))
    assert all(e.labels["quality"] == "good" for e in relabeled)
    other = Dataset.from_examples("o", [_ex("c")])
    assert len(ds.concat(other, name="merged")) == 3


def test_dataset_content_hash_is_ordered() -> None:
    # Two examples with *distinct content* (different output → different hash).
    x = _ex("a", output="x")
    y = _ex("b", output="y")
    a = Dataset.from_examples("d", [x, y])
    b = Dataset.from_examples("d", [y, x])
    # Order matters for a dataset's identity (it's an ordered collection).
    assert a.content_hash != b.content_hash
    assert a.content_hash == Dataset.from_examples("d", [x, y]).content_hash


def test_to_record_is_json_able() -> None:
    rec = _ex(qa=QAVerdict(passed=False, score=0.2)).to_record()
    assert rec["role"] == "adapter"
    assert rec["qa"]["passed"] is False
    assert "content_hash" in rec
