"""The composable report model — a renderer-agnostic document description.

A :class:`Report` is an ordered tree of immutable blocks. Each block is a small
frozen dataclass that knows *what* it is, never *how* it draws. Renderers
(:mod:`app.reports.render`) walk this tree; charts (:mod:`app.reports.charts`)
turn :class:`Chart` specs into SVG. Because the model is pure data it round-trips
to JSON losslessly (:meth:`Report.to_dict` / :func:`block_from_dict`), which is
both the machine-readable export format *and* the wire payload for the API.

Design rules that keep the golden-file tests honest:

* **Immutable + ordered.** Blocks are frozen; lists are tuples after
  construction. Two identical inputs always build an identical tree.
* **No floats where a string was given.** Numbers carry an optional pre-formatted
  display string (``Stat.display``) so the *builder* decides precision once and
  every renderer agrees byte-for-byte.
* **Self-describing tables.** A :class:`Table` carries typed
  :class:`TableColumn`\\ s (label, alignment, kind) so the same table renders to
  HTML, a PDF grid, *and* a CSV without the renderer guessing.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# --------------------------------------------------------------------------- #
# Enumerations (small, string-valued so they serialize cleanly)
# --------------------------------------------------------------------------- #


class Align(enum.StrEnum):
    """Horizontal alignment for a table column / key-value value."""

    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


class ColumnKind(enum.StrEnum):
    """The semantic kind of a table column (drives alignment + CSV coercion)."""

    TEXT = "text"
    NUMBER = "number"
    PERCENT = "percent"
    SECONDS = "seconds"
    DATE = "date"


class CalloutTone(enum.StrEnum):
    """The emphasis of a callout box (maps to a palette accent)."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    NEUTRAL = "neutral"


class BadgeTone(enum.StrEnum):
    """The emphasis of an inline badge / pill."""

    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    DANGER = "danger"
    NEUTRAL = "neutral"
    ACCENT = "accent"


class ChartKind(enum.StrEnum):
    """The chart families the SVG renderer supports (no new deps)."""

    BAR = "bar"
    GROUPED_BAR = "grouped_bar"
    LINE = "line"
    AREA = "area"
    PIE = "pie"
    DONUT = "donut"
    SPARKLINE = "sparkline"
    PROGRESS = "progress"


def align_of(kind: ColumnKind) -> Align:
    """The natural alignment for a column kind (numbers right, text left)."""
    if kind in (ColumnKind.NUMBER, ColumnKind.PERCENT, ColumnKind.SECONDS):
        return Align.RIGHT
    return Align.LEFT


# --------------------------------------------------------------------------- #
# Leaf value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Stat:
    """A single number with an optional pre-formatted display string.

    The *builder* formats once (``display``) so every renderer agrees on
    precision and units; ``value`` stays a raw float for CSV/JSON consumers that
    want to re-aggregate. When ``display`` is omitted, renderers fall back to a
    plain ``repr`` of ``value`` (used only by tests that pass raw numbers).
    """

    value: float
    display: str | None = None
    unit: str | None = None

    def text(self) -> str:
        """The human string for this stat (display if set, else value+unit)."""
        if self.display is not None:
            return self.display
        if self.unit:
            return f"{self.value:g}{self.unit}"
        return f"{self.value:g}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"value": self.value}
        if self.display is not None:
            d["display"] = self.display
        if self.unit is not None:
            d["unit"] = self.unit
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Stat:
        return Stat(
            value=float(d["value"]),
            display=d.get("display"),
            unit=d.get("unit"),
        )


@dataclass(frozen=True, slots=True)
class Series:
    """A named numeric series for charts (and its optional categorical labels)."""

    name: str
    values: tuple[float, ...]
    color: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"name": self.name, "values": list(self.values)}
        if self.color is not None:
            d["color"] = self.color
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Series:
        return Series(
            name=str(d["name"]),
            values=tuple(float(v) for v in d.get("values", ())),
            color=d.get("color"),
        )


# --------------------------------------------------------------------------- #
# Blocks — the renderable units
# --------------------------------------------------------------------------- #


class Block:
    """Marker base for every renderable block (kept tiny on purpose)."""

    #: Discriminator written into ``to_dict`` / read by :func:`block_from_dict`.
    block_type: str = "block"

    def to_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class Heading(Block):
    """A heading. ``level`` 1–4; 1 is the section title size."""

    block_type = "heading"
    text: str
    level: int = 2

    def to_dict(self) -> dict[str, Any]:
        return {"block_type": self.block_type, "text": self.text, "level": self.level}


@dataclass(frozen=True, slots=True)
class Paragraph(Block):
    """A run of prose. ``muted`` renders in the secondary text color."""

    block_type = "paragraph"
    text: str
    muted: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"block_type": self.block_type, "text": self.text, "muted": self.muted}


@dataclass(frozen=True, slots=True)
class KeyValueItem:
    """One row of a key/value grid (a label + a stat)."""

    label: str
    stat: Stat
    emphasis: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "stat": self.stat.to_dict(),
            "emphasis": self.emphasis,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> KeyValueItem:
        return KeyValueItem(
            label=str(d["label"]),
            stat=Stat.from_dict(d["stat"]),
            emphasis=bool(d.get("emphasis", False)),
        )


@dataclass(frozen=True, slots=True)
class KeyValue(Block):
    """A grid of headline stats (the "big numbers" strip)."""

    block_type = "keyvalue"
    items: tuple[KeyValueItem, ...]
    columns: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "block_type": self.block_type,
            "items": [i.to_dict() for i in self.items],
            "columns": self.columns,
        }


@dataclass(frozen=True, slots=True)
class TableColumn:
    """A typed table column header."""

    key: str
    label: str
    kind: ColumnKind = ColumnKind.TEXT
    align: Align | None = None

    def alignment(self) -> Align:
        """Explicit alignment if set, else the natural one for the kind."""
        return self.align if self.align is not None else align_of(self.kind)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": str(self.kind),
            "align": str(self.alignment()),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> TableColumn:
        return TableColumn(
            key=str(d["key"]),
            label=str(d["label"]),
            kind=ColumnKind(d.get("kind", "text")),
            align=Align(d["align"]) if d.get("align") else None,
        )


@dataclass(frozen=True, slots=True)
class Table(Block):
    """A typed table: columns + rows of cell strings keyed by column key.

    Cells are *strings* — the builder formats them so every renderer (and the CSV
    export) is byte-identical. ``total_row`` is an optional emphasized footer.
    """

    block_type = "table"
    columns: tuple[TableColumn, ...]
    rows: tuple[dict[str, str], ...]
    caption: str | None = None
    total_row: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "block_type": self.block_type,
            "columns": [c.to_dict() for c in self.columns],
            "rows": [dict(r) for r in self.rows],
        }
        if self.caption is not None:
            d["caption"] = self.caption
        if self.total_row is not None:
            d["total_row"] = dict(self.total_row)
        return d


@dataclass(frozen=True, slots=True)
class Chart(Block):
    """A chart specification (rendered to SVG by :mod:`app.reports.charts`)."""

    block_type = "chart"
    kind: ChartKind
    series: tuple[Series, ...]
    labels: tuple[str, ...] = ()
    title: str | None = None
    height: int = 220
    #: For pie/donut/progress, the single number band; chart-specific extras.
    options: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "block_type": self.block_type,
            "kind": str(self.kind),
            "series": [s.to_dict() for s in self.series],
            "labels": list(self.labels),
            "height": self.height,
        }
        if self.title is not None:
            d["title"] = self.title
        if self.options:
            d["options"] = dict(self.options)
        return d


@dataclass(frozen=True, slots=True)
class Callout(Block):
    """A boxed note (tone-colored) — for verdicts / context / warnings."""

    block_type = "callout"
    text: str
    tone: CalloutTone = CalloutTone.INFO
    title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "block_type": self.block_type,
            "text": self.text,
            "tone": str(self.tone),
        }
        if self.title is not None:
            d["title"] = self.title
        return d


@dataclass(frozen=True, slots=True)
class Badge(Block):
    """A standalone pill — e.g. a PASS/FAIL verdict on its own line."""

    block_type = "badge"
    text: str
    tone: BadgeTone = BadgeTone.NEUTRAL

    def to_dict(self) -> dict[str, Any]:
        return {"block_type": self.block_type, "text": self.text, "tone": str(self.tone)}


@dataclass(frozen=True, slots=True)
class Divider(Block):
    """A horizontal rule."""

    block_type = "divider"

    def to_dict(self) -> dict[str, Any]:
        return {"block_type": self.block_type}


@dataclass(frozen=True, slots=True)
class Spacer(Block):
    """Vertical whitespace, ``size`` in abstract units (≈ points)."""

    block_type = "spacer"
    size: int = 12

    def to_dict(self) -> dict[str, Any]:
        return {"block_type": self.block_type, "size": self.size}


@dataclass(frozen=True, slots=True)
class Section:
    """A titled group of blocks. ``page_break_before`` hints the PDF paginator."""

    title: str | None
    blocks: tuple[Block, ...]
    page_break_before: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "page_break_before": self.page_break_before,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Section:
        return Section(
            title=d.get("title"),
            blocks=tuple(block_from_dict(b) for b in d.get("blocks", ())),
            page_break_before=bool(d.get("page_break_before", False)),
        )


@dataclass(frozen=True, slots=True)
class ReportMeta:
    """Document metadata (cover page + filename + provenance)."""

    title: str
    subtitle: str | None = None
    kind: str = "generic"
    subject: str | None = None
    generated_at: str | None = None
    footer: str | None = None
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "kind": self.kind,
            "subject": self.subject,
            "generated_at": self.generated_at,
            "footer": self.footer,
            "tags": list(self.tags),
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> ReportMeta:
        return ReportMeta(
            title=str(d["title"]),
            subtitle=d.get("subtitle"),
            kind=str(d.get("kind", "generic")),
            subject=d.get("subject"),
            generated_at=d.get("generated_at"),
            footer=d.get("footer"),
            tags=tuple(d.get("tags", ())),
        )


@dataclass(frozen=True, slots=True)
class Report:
    """A complete document: metadata + ordered sections."""

    meta: ReportMeta
    sections: tuple[Section, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "meta": self.meta.to_dict(),
            "sections": [s.to_dict() for s in self.sections],
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Report:
        return Report(
            meta=ReportMeta.from_dict(d["meta"]),
            sections=tuple(Section.from_dict(s) for s in d.get("sections", ())),
        )

    def iter_blocks(self) -> list[Block]:
        """Flatten every block across every section (renderer convenience)."""
        out: list[Block] = []
        for section in self.sections:
            out.extend(section.blocks)
        return out

    def tables(self) -> list[Table]:
        """Every :class:`Table` in document order (for the CSV export)."""
        return [b for b in self.iter_blocks() if isinstance(b, Table)]


# --------------------------------------------------------------------------- #
# Deserialization dispatch
# --------------------------------------------------------------------------- #

_BLOCK_BUILDERS: dict[str, Callable[[dict[str, Any]], Block]] = {}


def _register() -> None:
    """Populate the ``block_type -> from_dict`` dispatch table once."""
    _BLOCK_BUILDERS.update(
        {
            "heading": lambda d: Heading(text=d["text"], level=int(d.get("level", 2))),
            "paragraph": lambda d: Paragraph(
                text=d["text"], muted=bool(d.get("muted", False))
            ),
            "keyvalue": lambda d: KeyValue(
                items=tuple(KeyValueItem.from_dict(i) for i in d.get("items", ())),
                columns=int(d.get("columns", 3)),
            ),
            "table": lambda d: Table(
                columns=tuple(TableColumn.from_dict(c) for c in d.get("columns", ())),
                rows=tuple(dict(r) for r in d.get("rows", ())),
                caption=d.get("caption"),
                total_row=dict(d["total_row"]) if d.get("total_row") else None,
            ),
            "chart": lambda d: Chart(
                kind=ChartKind(d["kind"]),
                series=tuple(Series.from_dict(s) for s in d.get("series", ())),
                labels=tuple(d.get("labels", ())),
                title=d.get("title"),
                height=int(d.get("height", 220)),
                options=dict(d.get("options", {})),
            ),
            "callout": lambda d: Callout(
                text=d["text"],
                tone=CalloutTone(d.get("tone", "info")),
                title=d.get("title"),
            ),
            "badge": lambda d: Badge(text=d["text"], tone=BadgeTone(d.get("tone", "neutral"))),
            "divider": lambda _d: Divider(),
            "spacer": lambda d: Spacer(size=int(d.get("size", 12))),
        }
    )


_register()


def block_from_dict(d: dict[str, Any]) -> Block:
    """Reconstruct a :class:`Block` from its ``to_dict`` form.

    Raises:
        ValueError: on an unknown ``block_type`` (a corrupt / future payload).
    """
    bt = str(d.get("block_type", ""))
    builder = _BLOCK_BUILDERS.get(bt)
    if builder is None:
        raise ValueError(f"unknown report block_type: {bt!r}")
    return builder(d)


__all__ = [
    "Align",
    "Badge",
    "BadgeTone",
    "Block",
    "Callout",
    "CalloutTone",
    "Chart",
    "ChartKind",
    "ColumnKind",
    "Divider",
    "Heading",
    "KeyValue",
    "KeyValueItem",
    "Paragraph",
    "Report",
    "ReportMeta",
    "Section",
    "Series",
    "Spacer",
    "Stat",
    "Table",
    "TableColumn",
    "align_of",
    "block_from_dict",
]
