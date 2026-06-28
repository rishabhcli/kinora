"""CSV renderer — every table in the report, flattened to one spreadsheet.

A report can hold several tables; the CSV export stacks them with a blank-line
separator and a ``# <caption>`` comment header per table so the result opens
cleanly in any spreadsheet while staying one downloadable file. Cells are already
strings (the builder formatted them), so the renderer just quotes correctly.

Uses the stdlib :mod:`csv` writer with ``\\r\\n`` line endings (RFC 4180) so the
output is deterministic across platforms.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from app.reports.model import Report, Table

#: The stdlib ``csv`` writer object (``csv._writer`` is not exposed for typing).
_Writer = Any


def _write_table(writer: _Writer, table: Table) -> None:
    if table.caption:
        writer.writerow([f"# {table.caption}"])
    writer.writerow([c.label for c in table.columns])
    keys = [c.key for c in table.columns]
    for row in table.rows:
        writer.writerow([row.get(k, "") for k in keys])
    if table.total_row is not None:
        writer.writerow([table.total_row.get(k, "") for k in keys])


def render_csv(report: Report) -> str:
    """Flatten every table in ``report`` to a single CSV document.

    With no tables, returns a one-line comment so the file is never empty (an
    empty CSV confuses some spreadsheet importers).
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    tables = report.tables()
    if not tables:
        writer.writerow([f"# {report.meta.title} — no tabular data"])
        return buf.getvalue()
    for i, table in enumerate(tables):
        if i > 0:
            writer.writerow([])  # blank separator row
        _write_table(writer, table)
    return buf.getvalue()


__all__ = ["render_csv"]
