"""Kinora reporting & document-generation subsystem.

A self-contained, dependency-light subsystem that turns the data Kinora already
holds into **documents a human reads** — reader-facing keepsakes (reading
progress, completion certificates, a year-in-review, a highlights digest) and
operator dashboards (budget burn, the §13 quality proof, render throughput).

The design is four clean layers, each independently testable:

1. **A composable report model** (:mod:`app.reports.model`). A :class:`Report`
   is an ordered list of *blocks* — headings, paragraphs, key/value grids,
   tables, charts, callouts, dividers, spacers, badges — grouped into
   :class:`Section`\\ s. Nothing in the model knows how it will be rendered; it
   is a pure, immutable, JSON-round-trippable description of a document.

2. **A theme/branding layer** (:mod:`app.reports.theme`). A :class:`Brand`
   carries the palette, type scale, and logo so the *same* report renders in
   Kinora's house style or a white-labelled one without touching the content.

3. **A pure-Python SVG chart renderer** (:mod:`app.reports.charts`). Bar, line,
   area, pie/donut, sparkline, and progress visuals are emitted as deterministic
   SVG strings with **no new dependency** — the same charts embed in the HTML and
   rasterise into the PDF.

4. **Multi-format renderers** (:mod:`app.reports.render`). One report renders to
   **HTML** (self-contained, themed), **PDF** (PyMuPDF, the existing dep),
   **CSV** (every table, flattened), and **JSON** (the model verbatim, the
   machine contract). Rendering is deterministic so the same report yields
   byte-identical output across runs — which is what the golden-file tests pin.

On top of those sit the **builders** (:mod:`app.reports.builders`) that read
Kinora's data through narrow read-only seams (:mod:`app.reports.sources`) and
assemble a :class:`Report`, the **artifact storage + signed retrieval**
(:mod:`app.reports.storage`), and the **service** (:mod:`app.reports.service`)
that orchestrates on-demand and scheduled generation.

Everything here spends **zero video-seconds** and makes no model calls — it reads
already-computed numbers and renders them. It is import-safe with no infra
(constructing the renderers opens no sockets), so the unit + golden suites run
anywhere.
"""

from __future__ import annotations

from app.reports.model import (
    Badge,
    BadgeTone,
    Block,
    Callout,
    CalloutTone,
    Chart,
    Divider,
    Heading,
    KeyValue,
    KeyValueItem,
    Paragraph,
    Report,
    ReportMeta,
    Section,
    Spacer,
    Table,
    TableColumn,
    align_of,
)
from app.reports.theme import Brand, Palette, TypeScale, default_brand

__all__ = [
    "Badge",
    "BadgeTone",
    "Block",
    "Brand",
    "Callout",
    "CalloutTone",
    "Chart",
    "Divider",
    "Heading",
    "KeyValue",
    "KeyValueItem",
    "Palette",
    "Paragraph",
    "Report",
    "ReportMeta",
    "Section",
    "Spacer",
    "Table",
    "TableColumn",
    "TypeScale",
    "align_of",
    "default_brand",
]
