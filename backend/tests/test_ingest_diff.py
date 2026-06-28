"""Incremental re-ingest diff unit tests (§9.1) — pure, no infra."""

from __future__ import annotations

from app.ingest.diff import (
    IngestDiff,
    PageChange,
    diff_pages,
    normalize_text,
    should_full_reingest,
    text_hash,
)

# --------------------------------------------------------------------------- #
# Normalisation + hashing
# --------------------------------------------------------------------------- #


def test_normalize_collapses_whitespace_and_case() -> None:
    assert normalize_text("  The   QUICK\nfox ") == "the quick fox"
    assert normalize_text(None) == ""


def test_text_hash_stable_for_reflow_variants() -> None:
    a = "The quick brown fox.\nJumped over."
    b = "the   quick brown   fox. jumped over."
    assert text_hash(a) == text_hash(b)


def test_text_hash_differs_on_real_change() -> None:
    assert text_hash("the cat sat") != text_hash("the dog sat")


# --------------------------------------------------------------------------- #
# diff_pages
# --------------------------------------------------------------------------- #


def test_identical_books_no_change() -> None:
    old = {1: "page one", 2: "page two"}
    new = {1: "page one", 2: "page two"}
    diff = diff_pages(old, new)
    assert diff.is_identical
    assert diff.unchanged == [1, 2]
    assert diff.to_reanalyze == []


def test_changed_page_detected() -> None:
    old = {1: "page one", 2: "page two"}
    new = {1: "page one", 2: "page two REVISED"}
    diff = diff_pages(old, new)
    assert diff.changed == [2]
    assert diff.unchanged == [1]
    assert not diff.is_identical
    assert diff.to_reanalyze == [2]


def test_added_and_removed_pages() -> None:
    old = {1: "a", 2: "b", 3: "c"}
    new = {1: "a", 2: "b", 4: "d"}  # 3 removed, 4 added
    diff = diff_pages(old, new)
    assert diff.removed == [3]
    assert diff.added == [4]
    assert diff.unchanged == [1, 2]
    assert diff.to_reanalyze == [4]


def test_diff_classifies_every_page_once() -> None:
    old = {1: "a", 2: "b"}
    new = {2: "b changed", 3: "new"}
    diff = diff_pages(old, new)
    verdicts = {p.page_number: p.change for p in diff.pages}
    assert verdicts == {
        1: PageChange.REMOVED,
        2: PageChange.CHANGED,
        3: PageChange.ADDED,
    }
    assert diff.num_pages_changed == 3


def test_empty_old_means_all_added() -> None:
    diff = diff_pages({}, {1: "x", 2: "y"})
    assert diff.added == [1, 2]
    assert diff.to_reanalyze == [1, 2]


# --------------------------------------------------------------------------- #
# should_full_reingest
# --------------------------------------------------------------------------- #


def test_small_edit_stays_incremental() -> None:
    old: dict[int, str | None] = {i: f"page {i}" for i in range(1, 11)}
    new: dict[int, str | None] = dict(old)
    new[5] = "page 5 edited"
    diff = diff_pages(old, new)
    assert should_full_reingest(diff) is False


def test_large_change_triggers_full_reingest() -> None:
    old: dict[int, str | None] = {i: f"page {i}" for i in range(1, 11)}
    new: dict[int, str | None] = {i: f"page {i} totally different" for i in range(1, 11)}
    diff = diff_pages(old, new)
    assert should_full_reingest(diff) is True


def test_empty_diff_is_not_full_reingest() -> None:
    assert should_full_reingest(IngestDiff(pages=[])) is False
