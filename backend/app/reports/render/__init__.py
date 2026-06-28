"""Multi-format renderers for a :class:`~app.reports.model.Report`.

One report, four outputs — each a pure function ``(report, brand) -> bytes/str``:

* :func:`~app.reports.render.json_render.render_json` — the model verbatim, the
  machine-readable contract (round-trips via :meth:`Report.from_dict`).
* :func:`~app.reports.render.csv_render.render_csv` — every table flattened, the
  spreadsheet export.
* :func:`~app.reports.render.html.render_html` — a self-contained, themed HTML
  document with inline SVG charts.
* :func:`~app.reports.render.pdf.render_pdf` — a paginated PDF via PyMuPDF (the
  existing dependency), charts rasterised from the same SVG.

A small :class:`MediaType` registry maps a format name to its content type +
extension, which the service + API reuse.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.reports.model import Report
from app.reports.render.csv_render import render_csv
from app.reports.render.html import render_html
from app.reports.render.json_render import render_json
from app.reports.render.pdf import render_pdf
from app.reports.theme import Brand, default_brand


class ReportFormat(enum.StrEnum):
    """The output formats a report can be rendered to."""

    PDF = "pdf"
    HTML = "html"
    CSV = "csv"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class MediaType:
    """Content type + file extension for a rendered artifact."""

    content_type: str
    extension: str


_MEDIA: dict[ReportFormat, MediaType] = {
    ReportFormat.PDF: MediaType("application/pdf", "pdf"),
    ReportFormat.HTML: MediaType("text/html; charset=utf-8", "html"),
    ReportFormat.CSV: MediaType("text/csv; charset=utf-8", "csv"),
    ReportFormat.JSON: MediaType("application/json; charset=utf-8", "json"),
}


def media_type(fmt: ReportFormat) -> MediaType:
    """The :class:`MediaType` for a format."""
    return _MEDIA[fmt]


def render(report: Report, fmt: ReportFormat, brand: Brand | None = None) -> bytes:
    """Render ``report`` to ``fmt`` and return the artifact bytes.

    A single dispatch so the service never branches on format. HTML/CSV/JSON are
    UTF-8 encoded; PDF is already bytes.
    """
    b = brand or default_brand()
    if fmt is ReportFormat.PDF:
        return render_pdf(report, b)
    if fmt is ReportFormat.HTML:
        return render_html(report, b).encode("utf-8")
    if fmt is ReportFormat.CSV:
        return render_csv(report).encode("utf-8")
    if fmt is ReportFormat.JSON:
        return render_json(report).encode("utf-8")
    raise ValueError(f"unsupported report format: {fmt!r}")  # pragma: no cover


__all__ = [
    "MediaType",
    "ReportFormat",
    "media_type",
    "render",
    "render_csv",
    "render_html",
    "render_json",
    "render_pdf",
]
