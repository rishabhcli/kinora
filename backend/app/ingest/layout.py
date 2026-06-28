"""Layout / column / reading-order analysis over PyMuPDF word tuples (§9.1 step 1).

PyMuPDF's ``page.get_text("words", sort=True)`` sorts words **top-to-bottom then
left-to-right**, which is correct for a single text column but *interleaves*
columns on a multi-column page (academic PDFs, magazines, two-up scans): a line
of the left column is immediately followed by the line of the right column at the
same vertical band, so the reconstructed reading order zig-zags across the gutter
and the book-global word index — the key the §4.2 source-span index sorts on —
ends up scrambled relative to how a human reads.

This module is the fix. It is a **pure** transform over the raw word tuples
``(x0, y0, x1, y1, text, block_no, line_no, word_no)`` that:

1. **detects the column layout** of a page by projecting word x-spans onto the
   horizontal axis and finding wide vertical "valleys" (gutters) with no ink;
2. **assigns each word to a column** and re-threads reading order **column by
   column, top to bottom within each column** — the order a person actually
   reads;
3. is a no-op (identity order) for a single-column page, so the common case is
   never perturbed.

The result feeds :func:`app.ingest.pdf_extract._extract_page`, which assigns the
book-global ``word_index`` in this corrected order. Everything downstream
(reconciliation, the source-span index, karaoke) inherits the right order for
free. No DB, no network, no object store — trivially unit-testable.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

from app.core.logging import get_logger

logger = get_logger("app.ingest.layout")

#: A page narrower than this (points) is never treated as multi-column.
_MIN_PAGE_WIDTH = 200.0
#: A gutter must be at least this fraction of page width to split columns — stops
#: ordinary inter-word spacing from being mistaken for a column boundary.
_MIN_GUTTER_FRACTION = 0.045
#: Never infer more than this many columns (defends against pathological inputs).
_MAX_COLUMNS = 6
#: Words whose vertical centres are within this fraction of page height are one
#: "line" for the within-column ordering tie-break.
_LINE_BAND_FRACTION = 0.012


class Word(NamedTuple):
    """A normalised word tuple (a subset of PyMuPDF's ``words`` row).

    Only the geometry + text are needed for layout; ``block``/``line``/``word``
    indices are retained as a stable secondary sort key.
    """

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int
    line: int
    word: int

    @property
    def cx(self) -> float:
        """Horizontal centre."""
        return (self.x0 + self.x1) / 2.0

    @property
    def cy(self) -> float:
        """Vertical centre."""
        return (self.y0 + self.y1) / 2.0


@dataclass(frozen=True, slots=True)
class Column:
    """A detected text column — a horizontal band ``[x_lo, x_hi)`` of the page."""

    index: int
    x_lo: float
    x_hi: float

    def contains(self, word: Word) -> bool:
        return self.x_lo <= word.cx < self.x_hi


@dataclass(frozen=True, slots=True)
class LayoutResult:
    """The reading-ordered words plus the columns that produced the order."""

    words: list[Word]
    columns: list[Column]

    @property
    def num_columns(self) -> int:
        return len(self.columns)

    @property
    def is_multi_column(self) -> bool:
        return len(self.columns) > 1


def to_words(raw: Sequence[Sequence[Any]]) -> list[Word]:
    """Coerce PyMuPDF ``get_text("words")`` rows into :class:`Word` (skips empties)."""
    out: list[Word] = []
    for row in raw:
        if len(row) < 5:
            continue
        text = str(row[4])
        if not text.strip():
            continue
        out.append(
            Word(
                x0=float(row[0]),
                y0=float(row[1]),
                x1=float(row[2]),
                y1=float(row[3]),
                text=text,
                block=int(row[5]) if len(row) > 5 else 0,
                line=int(row[6]) if len(row) > 6 else 0,
                word=int(row[7]) if len(row) > 7 else 0,
            )
        )
    return out


def _coverage_intervals(words: list[Word]) -> list[tuple[float, float]]:
    """Merge word x-spans into the set of horizontal intervals that contain ink."""
    spans = sorted((w.x0, w.x1) for w in words)
    merged: list[tuple[float, float]] = []
    for lo, hi in spans:
        if merged and lo <= merged[-1][1]:
            prev_lo, prev_hi = merged[-1]
            merged[-1] = (prev_lo, max(prev_hi, hi))
        else:
            merged.append((lo, hi))
    return merged


def detect_columns(words: list[Word], page_width: float) -> list[Column]:
    """Detect column bands by finding wide ink-free vertical gutters.

    Projects every word onto the x-axis, merges the covered intervals, and treats
    each gap wider than ``_MIN_GUTTER_FRACTION`` of the page as a gutter that
    splits two columns. Returns a single full-width column when no real gutter is
    found (the single-column common case).
    """
    if not words or page_width < _MIN_PAGE_WIDTH:
        return [Column(index=0, x_lo=0.0, x_hi=max(page_width, 1.0))]

    intervals = _coverage_intervals(words)
    if len(intervals) <= 1:
        return [Column(index=0, x_lo=0.0, x_hi=page_width)]

    min_gutter = page_width * _MIN_GUTTER_FRACTION
    # Boundaries: the midpoints of any gap between consecutive covered intervals
    # that is wider than the gutter threshold.
    boundaries: list[float] = []
    for (_, hi), (lo, _) in zip(intervals[:-1], intervals[1:], strict=True):
        gap = lo - hi
        if gap >= min_gutter:
            boundaries.append((hi + lo) / 2.0)
        if len(boundaries) >= _MAX_COLUMNS - 1:
            break

    if not boundaries:
        return [Column(index=0, x_lo=0.0, x_hi=page_width)]

    edges = [0.0, *boundaries, page_width]
    return [
        Column(index=i, x_lo=edges[i], x_hi=edges[i + 1]) for i in range(len(edges) - 1)
    ]


def _assign_column(word: Word, columns: list[Column]) -> int:
    """Pick the column whose band contains the word's centre (nearest as fallback)."""
    for col in columns:
        if col.contains(word):
            return col.index
    # Fallback: nearest column centre (a word straddling an edge).
    return min(
        columns,
        key=lambda c: abs(word.cx - (c.x_lo + c.x_hi) / 2.0),
    ).index


def reading_order(words: list[Word], page_width: float, page_height: float) -> LayoutResult:
    """Re-thread ``words`` into human reading order, column by column.

    Single-column pages are returned in a stable top-to-bottom / left-to-right
    order (identical to what ``sort=True`` already gives); multi-column pages are
    read each column fully before the next, which is the order the source-span
    index must use.
    """
    if not words:
        return LayoutResult(words=[], columns=detect_columns(words, page_width))

    columns = detect_columns(words, page_width)
    band = max(page_height * _LINE_BAND_FRACTION, 1.0)

    def sort_key(w: Word) -> tuple[int, int, float]:
        col = _assign_column(w, columns)
        # Quantise y into line-bands so words on the same visual line keep their
        # natural left-to-right order even with tiny baseline jitter.
        line_band = int(w.cy / band)
        return (col, line_band, w.x0)

    ordered = sorted(words, key=sort_key)
    if len(columns) > 1:
        logger.debug("ingest.layout.multi_column", columns=len(columns), words=len(words))
    return LayoutResult(words=ordered, columns=columns)


def order_raw_words(
    raw: Sequence[Sequence[Any]], page_width: float, page_height: float
) -> list[Word]:
    """Convenience: coerce raw PyMuPDF rows and return them in reading order."""
    return reading_order(to_words(raw), page_width, page_height).words


__all__ = [
    "Column",
    "LayoutResult",
    "Word",
    "detect_columns",
    "order_raw_words",
    "reading_order",
    "to_words",
]
