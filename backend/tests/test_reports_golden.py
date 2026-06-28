"""Golden-file tests — pin the deterministic rendered output byte-for-byte.

The text renderers (JSON, CSV, HTML) and the SVG chart engine are fully
deterministic, so their output is committed under ``tests/reports_golden/`` and
asserted exactly. This is the regression net for the whole subsystem: a change
that alters any rendered byte must consciously regenerate the goldens.

Regenerate after an intentional change with::

    KINORA_REGEN_GOLDEN=1 backend/.venv/bin/pytest tests/test_reports_golden.py

PDF is *not* golden-pinned on raw bytes (its compressed streams embed
library-version noise); :mod:`tests.test_reports_render` asserts on PDF structure
(page count + extracted text) instead.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.reports.builders import (
    build_budget_report,
    build_completion_certificate,
    build_quality_report,
    build_reading_progress_report,
)
from app.reports.charts import render_chart
from app.reports.model import Chart, ChartKind, Report, Series
from app.reports.render import ReportFormat, render
from app.reports.sources import BookProgress, BudgetSnapshot, QualitySnapshot
from app.reports.theme import certificate_brand, default_brand

GOLDEN_DIR = Path(__file__).parent / "reports_golden"
_REGEN = os.environ.get("KINORA_REGEN_GOLDEN") == "1"
NOW = datetime(2026, 6, 28, 12, 0, 0, tzinfo=UTC)


def _assert_golden(name: str, content: bytes) -> None:
    """Assert ``content`` matches the committed golden ``name`` (or regenerate)."""
    path = GOLDEN_DIR / name
    if _REGEN:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return
    assert path.exists(), f"missing golden {name}; regenerate with KINORA_REGEN_GOLDEN=1"
    expected = path.read_bytes()
    assert content == expected, f"golden mismatch for {name}"


# ------------------------------------------------------------------ fixtures


def _progress() -> BookProgress:
    return BookProgress(
        book_id="book-001",
        title="The Tinderbox",
        author="Hans Christian Andersen",
        num_pages=24,
        status="ready",
        furthest_word=4200,
        total_words=6000,
        accepted_shots=18,
        total_shots=20,
        watched_seconds=210.0,
        last_read_at=NOW,
    )


def _done_progress() -> BookProgress:
    return BookProgress(
        book_id="book-002",
        title="Aesop's Fable",
        author="Aesop",
        num_pages=8,
        status="ready",
        furthest_word=2000,
        total_words=2000,
        accepted_shots=10,
        total_shots=10,
        watched_seconds=95.0,
        last_read_at=NOW,
    )


def _budget() -> BudgetSnapshot:
    return BudgetSnapshot(
        ceiling_seconds=1650,
        committed_seconds=420,
        reserved_seconds=30,
        reservation_count=5,
        commit_count=40,
        release_count=3,
        by_book=(("book-001", 300.0), ("book-002", 120.0)),
    )


def _quality() -> QualitySnapshot:
    return QualitySnapshot(
        total_shots=20,
        accepted_shots=18,
        degraded_shots=1,
        conflict_shots=0,
        total_video_seconds=100.0,
        accepted_video_seconds=90.0,
        regen_count=3,
        defect_count=2,
        defects_by_kind=(("qa_fail", 2),),
        mean_ccs=0.91,
        mean_critic_score=0.88,
    )


# ------------------------------------------------------------- chart goldens

_CHART_CASES: list[tuple[str, Chart]] = [
    (
        "chart_bar",
        Chart(
            ChartKind.BAR,
            (Series("v", (10.0, 25.0, 40.0, 15.0)),),
            ("Q1", "Q2", "Q3", "Q4"),
            "Spend",
        ),
    ),
    (
        "chart_line",
        Chart(
            ChartKind.LINE,
            (
                Series("a", (0.8, 0.85, 0.9, 0.88)),
                Series("b", (0.6, 0.62, 0.59, 0.61)),
            ),
            title="CCS",
        ),
    ),
    (
        "chart_donut",
        Chart(
            ChartKind.DONUT,
            (Series("Accepted", (90.0,)), Series("Rejected", (10.0,))),
            title="Footage",
        ),
    ),
    ("chart_progress", Chart(ChartKind.PROGRESS, (Series("p", (0.62,)),), height=44)),
]


@pytest.mark.parametrize(("name", "chart"), _CHART_CASES)
def test_chart_svg_goldens(name: str, chart: Chart) -> None:
    svg = render_chart(chart, default_brand())
    _assert_golden(f"{name}.svg", svg.encode("utf-8"))


# ------------------------------------------------------ report goldens (text)


def _report_cases() -> dict[str, Report]:
    return {
        "reading_progress": build_reading_progress_report(
            _progress(), generated_at=NOW, reader_name="Ada Lovelace"
        ),
        "completion_certificate": build_completion_certificate(
            _done_progress(),
            generated_at=NOW,
            reader_name="Ada Lovelace",
            certificate_no="0042",
        ),
        "budget": build_budget_report(
            _budget(),
            generated_at=NOW,
            book_titles={"book-001": "The Tinderbox", "book-002": "Aesop's Fable"},
        ),
        "quality": build_quality_report(
            _quality(), generated_at=NOW, book_title="The Tinderbox"
        ),
    }


@pytest.mark.parametrize("case", sorted(_report_cases()))
@pytest.mark.parametrize("fmt", [ReportFormat.JSON, ReportFormat.CSV, ReportFormat.HTML])
def test_report_text_goldens(case: str, fmt: ReportFormat) -> None:
    report = _report_cases()[case]
    brand = certificate_brand() if case == "completion_certificate" else default_brand()
    data = render(report, fmt, brand)
    ext = {ReportFormat.JSON: "json", ReportFormat.CSV: "csv", ReportFormat.HTML: "html"}[fmt]
    _assert_golden(f"{case}.{ext}", data)


def test_report_render_is_stable_across_runs() -> None:
    """Belt-and-braces: identical input → identical bytes (the golden premise)."""
    report = build_quality_report(_quality(), generated_at=NOW)
    for fmt in (ReportFormat.JSON, ReportFormat.CSV, ReportFormat.HTML):
        assert render(report, fmt) == render(report, fmt)
