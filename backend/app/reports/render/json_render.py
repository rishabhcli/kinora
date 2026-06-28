"""JSON renderer — the report model serialized verbatim.

This is the machine-readable contract: ``render_json`` emits exactly
:meth:`Report.to_dict` so a consumer can round-trip it back via
:meth:`Report.from_dict`. Keys are sorted and indentation is fixed so the bytes
are deterministic (the golden-file tests pin them).
"""

from __future__ import annotations

import json

from app.reports.model import Report


def render_json(report: Report, *, indent: int = 2) -> str:
    """Serialize a report to deterministic, pretty JSON."""
    return json.dumps(
        report.to_dict(),
        indent=indent,
        sort_keys=True,
        ensure_ascii=False,
    )


__all__ = ["render_json"]
