"""Unit tests for the synthetic workload generator (no infra).

These also serve as cross-layer integration: a generated stream is fed to the
profiler, detector, and advisor to prove the layers compose."""

from __future__ import annotations

from collections import Counter

from app.datascale.optimize.advisor import IndexAdvisor
from app.datascale.optimize.nplusone import NPlusOneDetector
from app.datascale.optimize.profiler import QueryProfiler
from app.datascale.optimize.workloadgen import (
    QueryKind,
    WorkloadGenerator,
    WorkloadSpec,
)

# --------------------------------------------------------------------------- #
# Determinism (the property the test suite depends on)
# --------------------------------------------------------------------------- #


def test_same_seed_same_stream() -> None:
    g1 = WorkloadGenerator(seed=42)
    g2 = WorkloadGenerator(seed=42)
    s1 = g1.stream(200)
    s2 = g2.stream(200)
    assert [(q.kind, q.params, q.latency_ms) for q in s1] == [
        (q.kind, q.params, q.latency_ms) for q in s2
    ]


def test_different_seed_different_stream() -> None:
    s1 = WorkloadGenerator(seed=1).stream(200)
    s2 = WorkloadGenerator(seed=2).stream(200)
    assert s1 != s2


def test_reset_reproduces() -> None:
    g = WorkloadGenerator(seed=7)
    first = g.stream(100)
    g.reset()
    second = g.stream(100)
    assert first == second


# --------------------------------------------------------------------------- #
# Stream shape
# --------------------------------------------------------------------------- #


def test_stream_length_and_kinds() -> None:
    g = WorkloadGenerator(seed=1, spec=WorkloadSpec(n_queries=500))
    stream = g.stream()
    assert len(stream) == 500
    kinds = {q.kind for q in stream}
    # The dominant reading-room kinds should all appear at this volume.
    assert QueryKind.SHOT_BY_SOURCE_SPAN in kinds
    assert QueryKind.SHOT_BY_BOOK in kinds


def test_skew_concentrates_hot_books() -> None:
    # High skew -> a few books dominate the SHOT_BY_BOOK params.
    g = WorkloadGenerator(seed=3, spec=WorkloadSpec(n_books=50, skew=2.5))
    books = Counter(
        q.params[0] for q in g.stream(2000) if q.kind == QueryKind.SHOT_BY_BOOK
    )
    # Book 1 (hottest rank) should hold a large share.
    most_common_book, share = books.most_common(1)[0]
    assert most_common_book == 1
    assert share / sum(books.values()) > 0.3


def test_uniform_skew_spreads_books() -> None:
    g = WorkloadGenerator(seed=3, spec=WorkloadSpec(n_books=20, skew=0.0))
    books = Counter(
        q.params[0] for q in g.stream(4000) if q.kind == QueryKind.SHOT_BY_BOOK
    )
    # With no skew the hottest book holds a modest share.
    _, share = books.most_common(1)[0]
    assert share / sum(books.values()) < 0.2


def test_params_match_kind() -> None:
    g = WorkloadGenerator(seed=9)
    for q in g.stream(300):
        if q.kind == QueryKind.SHOT_BY_SOURCE_SPAN:
            assert len(q.params) == 2
        elif q.kind in (QueryKind.SHOT_COUNT_BY_BOOK, QueryKind.BOOK_LIST_RECENT):
            assert q.params == ()
        else:
            assert len(q.params) == 1


def test_table_sizes_match_spec() -> None:
    g = WorkloadGenerator(spec=WorkloadSpec(n_books=10, n_shots_per_book=100))
    sizes = g.table_sizes()
    assert sizes["book"] == 10
    assert sizes["shot"] == 1000
    assert sizes["continuity_state"] == 1000


# --------------------------------------------------------------------------- #
# Cross-layer composition
# --------------------------------------------------------------------------- #


def test_stream_feeds_profiler() -> None:
    g = WorkloadGenerator(seed=5, spec=WorkloadSpec(n_queries=1000))
    prof = QueryProfiler()
    for q in g.stream():
        prof.record(q.sql, q.latency_ms, rows=1)
    report = prof.report()
    # Distinct shapes collapse to the number of query kinds actually emitted.
    assert 1 <= len(report.shapes) <= len(QueryKind)
    assert report.total_calls == 1000
    # The count aggregate is the most expensive per-call; verify a report renders.
    assert report.as_dict()["total_calls"] == 1000


def test_workload_feeds_advisor() -> None:
    g = WorkloadGenerator(seed=11, spec=WorkloadSpec(n_queries=2000))
    wl = g.workload()
    advisor = IndexAdvisor(table_sizes=g.table_sizes())
    recs = advisor.recommend(wl)
    assert recs
    # The advisor should recommend an index on shot.book_id (the hot seek).
    tables = {r.candidate.table for r in recs}
    assert "shot" in tables
    rec_cols = {(r.candidate.table, r.candidate.columns) for r in recs}
    assert ("shot", ("book_id",)) in rec_cols or any(
        c[0] == "shot" and "book_id" in c[1] for c in rec_cols
    )


def test_n_plus_one_burst_is_detected() -> None:
    g = WorkloadGenerator(seed=2)
    det = NPlusOneDetector(threshold=10)
    for q in g.n_plus_one_burst(QueryKind.CONTINUITY_BY_SHOT, 50):
        det.observe(q.sql, params=q.params)
    findings = det.findings()
    assert len(findings) == 1
    assert findings[0].count == 50
    assert "continuity_state" in findings[0].skeleton


def test_workload_weights_reflect_frequency() -> None:
    g = WorkloadGenerator(seed=13, spec=WorkloadSpec(n_queries=3000))
    wl = g.workload()
    # Weights sum to the total query count (each event contributes 1).
    assert sum(q.weight for q in wl.queries) == 3000
    # Queries are ordered hottest-first.
    weights = [q.weight for q in wl.queries]
    assert weights == sorted(weights, reverse=True)
