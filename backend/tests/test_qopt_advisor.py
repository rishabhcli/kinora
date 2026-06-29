"""Unit tests for the index advisor (no infra)."""

from __future__ import annotations

import pytest

from app.datascale.optimize.advisor import (
    IndexAdvisor,
    IndexCandidate,
    Workload,
    candidates_for_shape,
)
from app.datascale.optimize.sqlshape import parse_select

# --------------------------------------------------------------------------- #
# Candidate generation
# --------------------------------------------------------------------------- #


def test_candidate_from_equality_predicate() -> None:
    shape = parse_select("SELECT * FROM shot WHERE book_id = $1")
    cands = candidates_for_shape(shape)
    assert IndexCandidate("shot", ("book_id",)) in cands


def test_candidate_equality_then_range_then_sort() -> None:
    shape = parse_select(
        "SELECT id FROM shot WHERE book_id = $1 AND page_no >= 5 ORDER BY page_no"
    )
    cands = candidates_for_shape(shape)
    # equality (book_id) then the range column (page_no); ORDER BY is the same
    # range column so it is not duplicated.
    assert IndexCandidate("shot", ("book_id", "page_no")) in cands


def test_candidate_order_by_extends_when_no_range() -> None:
    shape = parse_select("SELECT id FROM shot WHERE book_id = $1 ORDER BY created_at")
    cands = candidates_for_shape(shape)
    assert IndexCandidate("shot", ("book_id", "created_at")) in cands


def test_candidate_join_keys() -> None:
    shape = parse_select(
        "SELECT b.id FROM book b JOIN shot s ON s.book_id = b.id WHERE b.id = 1"
    )
    cands = candidates_for_shape(shape)
    tables = {(c.table, c.columns) for c in cands}
    # The join keys produce single-column indexes on each side's owning table.
    assert ("shot", ("book_id",)) in tables
    assert ("book", ("id",)) in tables


def test_unqualified_columns_in_multi_table_skipped() -> None:
    # Two tables, an unqualified predicate column cannot be attributed -> skipped.
    shape = parse_select("SELECT * FROM book b, shot s WHERE status = 'x'")
    cands = candidates_for_shape(shape)
    # No composite from the unattributable 'status'.
    assert all("status" not in c.columns for c in cands)


def test_index_candidate_name_and_ddl() -> None:
    c = IndexCandidate("shot", ("book_id", "page_no"))
    assert c.name == "ix_shot_book_id_page_no"
    ddl = c.ddl()
    assert ddl.startswith('CREATE INDEX CONCURRENTLY IF NOT EXISTS "ix_shot_book_id_page_no"')
    assert '"book_id", "page_no"' in ddl


def test_index_candidate_requires_columns() -> None:
    with pytest.raises(ValueError):
        IndexCandidate("shot", ())


def test_prefix_relationship() -> None:
    short = IndexCandidate("shot", ("book_id",))
    long = IndexCandidate("shot", ("book_id", "page_no"))
    assert short.is_prefix_of(long)
    assert not long.is_prefix_of(short)
    assert long.covers(short)
    assert long.covers(long)


# --------------------------------------------------------------------------- #
# Recommendation: ranking + pruning
# --------------------------------------------------------------------------- #


def test_recommend_ranks_by_benefit() -> None:
    wl = Workload()
    # Hot query (weight 100) on shot.book_id; cold query (weight 1) on book.id.
    wl.add("SELECT * FROM shot WHERE book_id = $1", weight=100)
    wl.add("SELECT * FROM book WHERE id = $1", weight=1)
    advisor = IndexAdvisor(table_sizes={"shot": 1_000_000, "book": 1000})
    recs = advisor.recommend(wl)
    assert recs
    # The hot, large-table index ranks first.
    assert recs[0].candidate.table == "shot"
    assert recs[0].benefit > recs[-1].benefit


def test_recommend_prunes_prefix_redundancy() -> None:
    wl = Workload()
    # One query wants (book_id); another wants (book_id, page_no).
    wl.add("SELECT * FROM shot WHERE book_id = $1", weight=10)
    wl.add("SELECT * FROM shot WHERE book_id = $1 AND page_no = 2", weight=10)
    advisor = IndexAdvisor(table_sizes={"shot": 1_000_000})
    recs = advisor.recommend(wl)
    keys = {r.candidate.columns for r in recs}
    # The single-column (book_id,) is a prefix of (book_id, page_no) -> pruned.
    assert ("book_id",) not in keys
    assert ("book_id", "page_no") in keys


def test_recommend_min_benefit_filter() -> None:
    wl = Workload()
    wl.add("SELECT * FROM tiny WHERE id = $1", weight=1)
    # A tiny table yields tiny benefit; a high bar filters it out.
    advisor = IndexAdvisor(table_sizes={"tiny": 10})
    assert advisor.recommend(wl, min_benefit=1e9) == []


def test_recommend_skips_index_that_cannot_serve() -> None:
    wl = Workload()
    # A LIKE predicate is not a seekable equality/range -> no candidate, no rec.
    wl.add("SELECT * FROM book WHERE title LIKE 'foo%'", weight=100)
    advisor = IndexAdvisor()
    assert advisor.recommend(wl) == []


def test_recommend_dedups_same_index_across_queries() -> None:
    wl = Workload()
    wl.add("SELECT * FROM shot WHERE book_id = 1", weight=5)
    wl.add("SELECT id FROM shot WHERE book_id = 2", weight=5)
    advisor = IndexAdvisor(table_sizes={"shot": 100_000})
    recs = advisor.recommend(wl)
    book_id_recs = [r for r in recs if r.candidate.columns == ("book_id",)]
    assert len(book_id_recs) == 1
    # Benefit summed across both queries; both support it.
    assert book_id_recs[0].supporting_queries == 2


def test_recommendation_as_dict() -> None:
    wl = Workload.from_pairs([("SELECT * FROM shot WHERE book_id = 1", 50.0)])
    advisor = IndexAdvisor(table_sizes={"shot": 500_000})
    rec = advisor.recommend(wl)[0]
    d = rec.as_dict()
    assert d["table"] == "shot"
    assert d["columns"] == ["book_id"]
    assert "ddl" in d
    assert d["supporting_queries"] == 1


def test_candidates_unranked_list() -> None:
    wl = Workload()
    wl.add("SELECT * FROM shot WHERE book_id = 1", weight=1)
    wl.add("SELECT * FROM book WHERE id = 1", weight=1)
    advisor = IndexAdvisor()
    cands = advisor.candidates(wl)
    tables = {c.table for c in cands}
    assert tables == {"shot", "book"}


def test_unshapable_queries_ignored() -> None:
    wl = Workload()
    wl.add("SELECT * FROM a UNION SELECT * FROM b", weight=100)  # unshapable
    wl.add("SELECT * FROM shot WHERE book_id = 1", weight=1)
    advisor = IndexAdvisor(table_sizes={"shot": 100_000})
    recs = advisor.recommend(wl)
    assert all(r.candidate.table == "shot" for r in recs)
