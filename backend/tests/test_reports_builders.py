"""Unit tests for the reader + operator report builders (pure functions)."""

from __future__ import annotations

from datetime import UTC, datetime

from app.reports.builders import (
    build_budget_report,
    build_completion_certificate,
    build_highlights_digest,
    build_library_overview_report,
    build_quality_report,
    build_reading_progress_report,
    build_throughput_report,
    build_year_in_review,
)
from app.reports.builders.operator import CCS_TARGET, EFFICIENCY_TARGET
from app.reports.model import Badge, BadgeTone, Chart, Report, Table
from app.reports.render import ReportFormat, render
from app.reports.sources import (
    BookProgress,
    BudgetSnapshot,
    LibrarySnapshot,
    QualitySnapshot,
    ReaderSummary,
    SceneRow,
    ThroughputSnapshot,
)

NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


def _progress(*, pct: float = 0.7, title: str = "Book") -> BookProgress:
    words = 1000
    return BookProgress(
        book_id="b1",
        title=title,
        author="Author",
        num_pages=20,
        status="ready",
        furthest_word=int(words * pct),
        total_words=words,
        accepted_shots=14,
        total_shots=16,
        watched_seconds=180.0,
        last_read_at=NOW,
    )


def _renders_everywhere(report: Report) -> None:
    for fmt in ReportFormat:
        out = render(report, fmt)
        assert out


def _all_blocks(report: Report) -> list:
    return report.iter_blocks()


# --------------------------------------------------------------------------- reader


def test_reading_progress_report_has_progress_and_renders() -> None:
    rep = build_reading_progress_report(_progress(pct=0.5), generated_at=NOW, reader_name="Ada")
    assert rep.meta.kind == "reading_progress"
    charts = [b for b in _all_blocks(rep) if isinstance(b, Chart)]
    assert charts and any(c.kind.value == "progress" for c in charts)
    _renders_everywhere(rep)


def test_completion_certificate_renders_and_names_reader() -> None:
    rep = build_completion_certificate(
        _progress(pct=1.0, title="The End"),
        generated_at=NOW,
        reader_name="Grace",
        certificate_no="7",
    )
    assert rep.meta.kind == "completion_certificate"
    html = render(rep, ReportFormat.HTML).decode()
    assert "Grace" in html
    assert "The End" in html
    assert any(isinstance(b, Badge) for b in _all_blocks(rep))
    _renders_everywhere(rep)


def test_year_in_review_counts_completed_and_renders() -> None:
    done = _progress(pct=1.0, title="Done")
    midway = _progress(pct=0.4, title="Mid")
    summary = ReaderSummary(user_id="u1", books=(done, midway))
    rep = build_year_in_review(summary, year=2026, generated_at=NOW, reader_name="Ada")
    assert "2026" in rep.meta.title
    tables = [b for b in _all_blocks(rep) if isinstance(b, Table)]
    # Finished-this-year table lists the completed book.
    assert tables and any("Done" in str(r.values()) for t in tables for r in t.rows)
    _renders_everywhere(rep)


def test_year_in_review_handles_empty_library() -> None:
    rep = build_year_in_review(
        ReaderSummary(user_id="u1", books=()), year=2026, generated_at=NOW
    )
    _renders_everywhere(rep)


def test_highlights_digest_lists_active_books() -> None:
    summary = ReaderSummary(user_id="u1", books=(_progress(pct=0.3, title="Active"),))
    rep = build_highlights_digest(summary, generated_at=NOW, reader_name="Ada")
    html = render(rep, ReportFormat.HTML).decode()
    assert "Active" in html
    _renders_everywhere(rep)


# --------------------------------------------------------------------------- operator


def test_budget_report_computes_remaining_and_top_spenders() -> None:
    snap = BudgetSnapshot(
        ceiling_seconds=1650,
        committed_seconds=400,
        reserved_seconds=50,
        reservation_count=4,
        commit_count=30,
        release_count=2,
        by_book=(("b1", 300.0), ("b2", 100.0)),
    )
    rep = build_budget_report(snap, generated_at=NOW, book_titles={"b1": "One", "b2": "Two"})
    assert rep.meta.kind == "budget"
    tables = [b for b in _all_blocks(rep) if isinstance(b, Table)]
    assert tables and tables[0].total_row is not None
    # Top-spender names resolved via the titles map.
    assert any("One" in str(r.values()) for t in tables for r in t.rows)
    _renders_everywhere(rep)


def test_budget_report_flags_exhaustion() -> None:
    snap = BudgetSnapshot(
        ceiling_seconds=100,
        committed_seconds=100,
        reserved_seconds=10,
        reservation_count=1,
        commit_count=10,
        release_count=0,
    )
    assert snap.remaining_seconds == 0.0
    rep = build_budget_report(snap, generated_at=NOW)
    _renders_everywhere(rep)


def test_quality_report_pass_badge_when_above_targets() -> None:
    snap = QualitySnapshot(
        total_shots=20,
        accepted_shots=19,
        degraded_shots=1,
        conflict_shots=0,
        total_video_seconds=100,
        accepted_video_seconds=95,
        regen_count=2,
        defect_count=1,
        defects_by_kind=(("qa_fail", 1),),
        mean_ccs=0.9,
    )
    assert snap.accepted_footage_efficiency >= EFFICIENCY_TARGET
    assert (snap.mean_ccs or 0) >= CCS_TARGET
    rep = build_quality_report(snap, generated_at=NOW)
    badges = [b for b in _all_blocks(rep) if isinstance(b, Badge)]
    assert badges and badges[0].tone is BadgeTone.SUCCESS
    _renders_everywhere(rep)


def test_quality_report_review_badge_when_below_targets() -> None:
    snap = QualitySnapshot(
        total_shots=20,
        accepted_shots=8,
        degraded_shots=8,
        conflict_shots=2,
        total_video_seconds=100,
        accepted_video_seconds=40,
        regen_count=12,
        defect_count=8,
        defects_by_kind=(("drift", 8),),
        mean_ccs=0.5,
    )
    rep = build_quality_report(snap, generated_at=NOW)
    badges = [b for b in _all_blocks(rep) if isinstance(b, Badge)]
    assert badges and badges[0].tone is BadgeTone.WARNING


def test_quality_report_with_baseline_adds_comparison() -> None:
    crew = QualitySnapshot(
        total_shots=20, accepted_shots=18, degraded_shots=1, conflict_shots=0,
        total_video_seconds=100, accepted_video_seconds=90, regen_count=3,
        defect_count=0, mean_ccs=0.91,
    )
    base = QualitySnapshot(
        total_shots=20, accepted_shots=12, degraded_shots=5, conflict_shots=1,
        total_video_seconds=100, accepted_video_seconds=62, regen_count=9,
        defect_count=0, mean_ccs=0.6,
    )
    rep = build_quality_report(crew, generated_at=NOW, baseline=base)
    titles = [s.title for s in rep.sections]
    assert "Crew vs baseline" in titles
    grouped = [
        b for b in _all_blocks(rep) if isinstance(b, Chart) and b.kind.value == "grouped_bar"
    ]
    assert grouped


def test_throughput_report_health_verdict() -> None:
    healthy = ThroughputSnapshot(
        jobs_total=50,
        by_status=(("succeeded", 48), ("cancelled", 2)),
        succeeded=48,
        deadletter=0,
        cancelled=2,
        mean_attempts=1.1,
        reserved_seconds_outstanding=0.0,
    )
    assert healthy.success_rate >= 0.9
    _renders_everywhere(build_throughput_report(healthy, generated_at=NOW))

    unhealthy = ThroughputSnapshot(
        jobs_total=10,
        by_status=(("deadletter", 5), ("succeeded", 5)),
        succeeded=5,
        deadletter=5,
        cancelled=0,
        mean_attempts=2.5,
        reserved_seconds_outstanding=20.0,
    )
    _renders_everywhere(build_throughput_report(unhealthy, generated_at=NOW))


def test_library_overview_with_scenes() -> None:
    lib = LibrarySnapshot(
        total_books=81,
        by_status=(("ready", 75), ("importing", 4), ("failed", 2)),
        total_shots=900,
        accepted_shots=810,
        total_users=12,
    )
    scenes = (
        SceneRow("s1", "Opening", 5, 5, 30.0, 30.0),
        SceneRow("s2", None, 8, 7, 48.0, 42.0),
    )
    rep = build_library_overview_report(lib, generated_at=NOW, scenes=scenes)
    titles = [s.title for s in rep.sections]
    assert "Per-scene spend" in titles
    _renders_everywhere(rep)
