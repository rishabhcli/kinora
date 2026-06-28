"""Behavioural tests for the four report renderers (HTML / CSV / JSON / PDF)."""

from __future__ import annotations

import json

import fitz
import pytest

from app.reports.model import (
    Badge,
    BadgeTone,
    Callout,
    CalloutTone,
    Chart,
    ChartKind,
    ColumnKind,
    Divider,
    Heading,
    KeyValue,
    KeyValueItem,
    Paragraph,
    Report,
    ReportMeta,
    Section,
    Series,
    Spacer,
    Stat,
    Table,
    TableColumn,
)
from app.reports.render import ReportFormat, media_type, render
from app.reports.render.csv_render import render_csv
from app.reports.render.html import render_html
from app.reports.render.json_render import render_json
from app.reports.render.pdf import render_pdf
from app.reports.theme import default_brand


def _report() -> Report:
    return Report(
        meta=ReportMeta(title="Render Test", subtitle="sub", kind="quality"),
        sections=(
            Section(
                title="S1",
                blocks=(
                    Heading("Numbers", level=1),
                    KeyValue(
                        items=(
                            KeyValueItem("A", Stat(1.0, "1")),
                            KeyValueItem("B", Stat(2.0, "2"), emphasis=True),
                        ),
                        columns=2,
                    ),
                    Paragraph("Some body text here.", muted=False),
                    Callout("verdict", tone=CalloutTone.SUCCESS, title="V"),
                    Badge("PASS", tone=BadgeTone.SUCCESS),
                    Divider(),
                    Spacer(10),
                    Chart(
                        kind=ChartKind.BAR,
                        series=(Series("v", (1.0, 2.0, 3.0)),),
                        labels=("x", "y", "z"),
                        title="bars",
                    ),
                ),
            ),
            Section(
                title="Tables",
                page_break_before=True,
                blocks=(
                    Table(
                        columns=(
                            TableColumn("name", "Name"),
                            TableColumn("n", "N", ColumnKind.NUMBER),
                        ),
                        rows=({"name": "alpha", "n": "10"}, {"name": "beta", "n": "20"}),
                        caption="A table",
                        total_row={"name": "Total", "n": "30"},
                    ),
                ),
            ),
        ),
    )


# --------------------------------------------------------------------------- JSON


def test_json_round_trips_to_model() -> None:
    rep = _report()
    parsed = Report.from_dict(json.loads(render_json(rep)))
    assert parsed.to_dict() == rep.to_dict()


def test_json_is_deterministic_and_sorted() -> None:
    rep = _report()
    a = render_json(rep)
    b = render_json(rep)
    assert a == b
    # Keys sorted: "meta" before "sections".
    assert a.index('"meta"') < a.index('"sections"')


# --------------------------------------------------------------------------- CSV


def test_csv_emits_every_table_with_header_and_total() -> None:
    csv_text = render_csv(_report())
    assert "Name,N" in csv_text
    assert "alpha,10" in csv_text
    assert "Total,30" in csv_text
    assert "# A table" in csv_text


def test_csv_uses_crlf_line_endings() -> None:
    assert "\r\n" in render_csv(_report())


def test_csv_no_tables_emits_comment() -> None:
    rep = Report(meta=ReportMeta(title="Empty"), sections=())
    out = render_csv(rep)
    assert out.startswith("# Empty")


# --------------------------------------------------------------------------- HTML


def test_html_is_self_contained_and_themed() -> None:
    html = render_html(_report(), default_brand())
    assert html.startswith("<!doctype html>")
    assert "<style>" in html  # inline CSS, no external link
    assert "link rel=\"stylesheet\"" not in html
    assert "Render Test" in html
    assert "<svg " in html  # inline chart
    assert "PASS" in html


def test_html_escapes_content() -> None:
    rep = Report(
        meta=ReportMeta(title="A & B <x>"),
        sections=(Section(None, (Paragraph("1 < 2 & 3"),)),),
    )
    html = render_html(rep, default_brand())
    assert "A &amp; B &lt;x&gt;" in html
    assert "1 &lt; 2 &amp; 3" in html


def test_html_is_deterministic() -> None:
    rep = _report()
    assert render_html(rep, default_brand()) == render_html(rep, default_brand())


# --------------------------------------------------------------------------- PDF


def test_pdf_renders_valid_multipage_document() -> None:
    data = render_pdf(_report(), default_brand())
    assert data[:5] == b"%PDF-"
    doc = fitz.open("pdf", data)
    try:
        # Page-break-before on section 2 forces at least 2 pages.
        assert doc.page_count >= 2
        text = "".join(doc[i].get_text() for i in range(doc.page_count))
        assert "Render Test" in text
        assert "Numbers" in text
        assert "alpha" in text
        assert "Total" in text
    finally:
        doc.close()


def test_pdf_metadata_carries_title() -> None:
    doc = fitz.open("pdf", render_pdf(_report(), default_brand()))
    try:
        assert doc.metadata.get("title") == "Render Test"
    finally:
        doc.close()


# --------------------------------------------------------------------------- dispatch


@pytest.mark.parametrize("fmt", list(ReportFormat))
def test_render_dispatch_returns_bytes_for_every_format(fmt: ReportFormat) -> None:
    data = render(_report(), fmt, default_brand())
    assert isinstance(data, bytes)
    assert len(data) > 0


def test_media_types() -> None:
    assert media_type(ReportFormat.PDF).content_type == "application/pdf"
    assert media_type(ReportFormat.CSV).extension == "csv"
    assert media_type(ReportFormat.HTML).content_type.startswith("text/html")
    assert media_type(ReportFormat.JSON).extension == "json"
