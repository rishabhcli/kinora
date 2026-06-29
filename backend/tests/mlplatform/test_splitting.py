"""Splitting: leak-free at group granularity, stratified, deterministic."""

from __future__ import annotations

import pytest

from app.mlplatform.datasets.contracts import AgentRole, Dataset, TaskType, TraceExample
from app.mlplatform.datasets.errors import SplitError
from app.mlplatform.datasets.splitting import (
    SplitConfig,
    SplitRatios,
    role_stratum,
    split_dataset,
)


def _ex(ex_id: str, book: str, role: AgentRole = AgentRole.ADAPTER) -> TraceExample:
    return TraceExample(
        id=ex_id,
        role=role,
        task=TaskType.SFT,
        prompt_key="adapter@v3",
        prompt_version="3.0.0",
        model="qwen-plus",
        input={"page_text": ex_id},
        output="o",
        book_id=book,
    )


def _dataset(n_books: int = 20, per_book: int = 3) -> Dataset:
    exs = [
        _ex(f"ex_{b}_{i}", f"bk{b}", AgentRole.ADAPTER if b % 2 else AgentRole.CRITIC)
        for b in range(n_books)
        for i in range(per_book)
    ]
    return Dataset.from_examples("d", exs)


def test_split_is_leak_free_by_book() -> None:
    ds, report = split_dataset(_dataset())
    assert report.leak_free
    # every book lands entirely in one split
    by_book: dict[str, set[str]] = {}
    for ex in ds.examples:
        by_book.setdefault(ex.book_id or "", set()).add(ex.split.value)
    assert all(len(splits) == 1 for splits in by_book.values())


def test_split_hits_ratios_roughly() -> None:
    ds, report = split_dataset(
        _dataset(n_books=30), config=SplitConfig(ratios=SplitRatios(0.6, 0.2, 0.2))
    )
    total = report.total
    assert report.counts["train"] > report.counts["val"]
    assert report.counts["train"] > report.counts["test"]
    assert sum(report.counts.values()) == total


def test_split_is_deterministic() -> None:
    ds = _dataset()
    a, _ = split_dataset(ds, config=SplitConfig(seed=7))
    b, _ = split_dataset(ds, config=SplitConfig(seed=7))
    assert {e.id: e.split for e in a.examples} == {e.id: e.split for e in b.examples}


def test_different_seed_changes_assignment() -> None:
    ds = _dataset()
    a, _ = split_dataset(ds, config=SplitConfig(seed=1))
    b, _ = split_dataset(ds, config=SplitConfig(seed=99))
    assert {e.id: e.split for e in a.examples} != {e.id: e.split for e in b.examples}


def test_stratify_balances_roles() -> None:
    ds, report = split_dataset(_dataset(n_books=40), config=SplitConfig(stratum_of=role_stratum))
    # both roles should appear in train (the stratifier spreads each class)
    train_roles = {e.role.value for e in ds.examples if e.split.value == "train"}
    assert "adapter" in train_roles and "critic" in train_roles


def test_bad_ratios_rejected() -> None:
    with pytest.raises(SplitError):
        SplitRatios(0.5, 0.4, 0.4)


def test_empty_dataset_rejected() -> None:
    with pytest.raises(SplitError):
        split_dataset(Dataset(name="empty", examples=()))
