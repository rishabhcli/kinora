"""Structured CLI output — one ``--format`` switch, two renderers.

Every action returns a result object exposing :meth:`Renderable.render_payload`,
which yields a :class:`Payload`: a JSON-serializable ``data`` blob plus a
``tables`` list describing how to lay the same data out for a human. The root
parser picks the format once; :func:`render` then either:

* ``json``  — pretty-prints ``payload.data`` (machine-consumable, the structured
  mode the task asks for), or
* ``table`` — draws each :class:`Table` as a monospace, pipe-free ASCII grid.

Keeping the *data* and the *table description* in one payload means an action is
defined once and renders identically in both modes — no drift between what a
human sees and what a script parses.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

Scalar = str | int | float | bool | None
# A JSON-serializable value. Typed as ``object`` rather than a recursive union so
# concrete result dicts pass through cleanly (mypy treats ``dict`` invariantly,
# which makes a recursive alias reject every concrete payload). The contract is
# "json.dumps can serialize it"; ``render_json`` enforces it at the boundary with
# ``default=str``.
JsonValue = object


class Format(StrEnum):
    """The two supported output formats."""

    TABLE = "table"
    JSON = "json"


@dataclass(frozen=True, slots=True)
class Table:
    """A titled grid: a header row of column names + a list of string rows.

    ``rows`` cells are pre-formatted strings (the action already applied the
    human formatting helpers). An empty ``rows`` renders as the title plus an
    ``(empty)`` marker so the operator sees the query ran and found nothing.
    """

    title: str
    columns: Sequence[str]
    rows: Sequence[Sequence[str]]


@dataclass(frozen=True, slots=True)
class Payload:
    """The dual-representation result of an action: JSON data + table views."""

    data: JsonValue
    tables: tuple[Table, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, data: JsonValue, *tables: Table) -> Payload:
        """Convenience constructor: ``Payload.of(data, table1, table2)``."""
        return cls(data=data, tables=tuple(tables))


@runtime_checkable
class Renderable(Protocol):
    """Anything an action returns — knows how to describe its own output."""

    def render_payload(self) -> Payload: ...


def _format_grid(columns: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    """Lay out a header + rows as space-padded, left-aligned columns."""
    str_rows = [[("" if c is None else str(c)) for c in row] for row in rows]
    widths = [len(str(col)) for col in columns]
    for row in str_rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], len(cell))
    out: list[str] = []
    header = "  ".join(str(col).ljust(widths[i]) for i, col in enumerate(columns))
    out.append(header)
    out.append("  ".join("-" * widths[i] for i in range(len(columns))))
    for row in str_rows:
        cells = []
        for i in range(len(columns)):
            cell = row[i] if i < len(row) else ""
            cells.append(cell.ljust(widths[i]))
        out.append("  ".join(cells).rstrip())
    return out


def render_table(payload: Payload) -> str:
    """Render a payload's tables as monospace ASCII grids."""
    blocks: list[str] = []
    if not payload.tables:
        # No table description — fall back to a compact JSON dump so nothing is
        # silently dropped in table mode.
        return render_json(payload)
    for table in payload.tables:
        lines = [table.title]
        if not table.rows:
            lines.append("  (empty)")
        else:
            lines.extend("  " + line for line in _format_grid(table.columns, table.rows))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def render_json(payload: Payload) -> str:
    """Pretty-print a payload's structured ``data`` as JSON."""
    return json.dumps(payload.data, indent=2, sort_keys=False, default=str)


def render(result: Renderable, fmt: Format) -> str:
    """Render any action result in the chosen format."""
    payload = result.render_payload()
    if fmt is Format.JSON:
        return render_json(payload)
    return render_table(payload)


def kv_table(title: str, mapping: dict[str, object]) -> Table:
    """Build a two-column key/value table from a mapping (for "inspect" views)."""
    rows = [[str(key), "" if value is None else str(value)] for key, value in mapping.items()]
    return Table(title=title, columns=("field", "value"), rows=rows)


__all__ = [
    "Format",
    "JsonValue",
    "Payload",
    "Renderable",
    "Scalar",
    "Table",
    "kv_table",
    "render",
    "render_json",
    "render_table",
]
