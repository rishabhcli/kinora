"""HTML renderer — a self-contained, themed document.

Walks the report tree and emits a single ``<!doctype html>`` string with an
inline ``<style>`` block derived from the :class:`~app.reports.theme.Brand`
(CSS custom properties), so the output has **no external dependency** — it opens
correctly from disk, in an email, or embedded in the desktop shell. Charts are
inlined as the same SVG the PDF rasterises, so HTML and PDF show identical
visuals.

Determinism: no timestamps are injected here (the builder stamps
``meta.generated_at``); attribute/order is fixed; so the same report yields the
same HTML, which the golden tests pin.
"""

from __future__ import annotations

import html

from app.reports.charts import render_chart
from app.reports.model import (
    Badge,
    Block,
    Callout,
    Chart,
    Divider,
    Heading,
    KeyValue,
    Paragraph,
    Report,
    Section,
    Spacer,
    Table,
)
from app.reports.theme import Brand


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _css(brand: Brand) -> str:
    p = brand.palette
    ts = brand.type_scale
    return f"""
:root {{
  --bg: {p.background}; --surface: {p.surface}; --surface-alt: {p.surface_alt};
  --border: {p.border}; --text: {p.text}; --muted: {p.text_muted};
  --heading: {p.heading}; --accent: {p.accent}; --accent-soft: {p.accent_soft};
  --info: {p.info}; --success: {p.success}; --warning: {p.warning}; --danger: {p.danger};
}}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font-family:{brand.font_family}; font-size:{ts.body}pt; line-height:1.55; }}
.kn-doc {{ max-width: 820px; margin: 0 auto; padding: 40px 28px 64px; }}
.kn-cover {{ display:flex; align-items:center; gap:16px; padding-bottom:18px;
  border-bottom:1px solid var(--border); margin-bottom:28px; }}
.kn-mark {{ color:var(--accent); flex:0 0 auto; }}
.kn-title {{ font-size:{ts.title}pt; font-weight:700; color:var(--heading);
  margin:0; letter-spacing:-0.5px; }}
.kn-subtitle {{ font-size:{ts.subtitle}pt; color:var(--muted); margin:4px 0 0; }}
.kn-brandline {{ font-size:{ts.small}pt; color:var(--muted); text-transform:uppercase;
  letter-spacing:1.5px; margin:0 0 2px; }}
.kn-section {{ margin: 30px 0; }}
.kn-section > h2.kn-section-title {{ font-size:{ts.h1}pt; color:var(--heading);
  margin:0 0 14px; font-weight:650; }}
h3.kn-h {{ font-size:{ts.h2}pt; color:var(--heading); margin:18px 0 8px; font-weight:600; }}
h4.kn-h {{ font-size:{ts.h3}pt; color:var(--heading); margin:14px 0 6px; font-weight:600; }}
h5.kn-h {{ font-size:{ts.h4}pt; color:var(--heading); margin:12px 0 6px; font-weight:600; }}
p.kn-p {{ margin: 8px 0; }}
p.kn-muted {{ color: var(--muted); }}
.kn-kv {{ display:grid; gap:14px; margin:14px 0; }}
.kn-kv-cell {{ background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:14px 16px; }}
.kn-kv-cell.kn-emph {{ border-color:var(--accent); }}
.kn-kv-stat {{ font-size:{ts.stat}pt; font-weight:700; color:var(--heading);
  line-height:1.1; }}
.kn-kv-label {{ font-size:{ts.stat_label}pt; color:var(--muted);
  text-transform:uppercase; letter-spacing:0.6px; margin-top:4px; }}
table.kn-table {{ width:100%; border-collapse:collapse; margin:12px 0;
  font-size:{ts.small}pt; }}
table.kn-table caption {{ caption-side:top; text-align:left; color:var(--muted);
  font-size:{ts.small}pt; margin-bottom:6px; }}
table.kn-table th {{ text-align:left; color:var(--muted); font-weight:600;
  padding:7px 10px; border-bottom:1px solid var(--border);
  text-transform:uppercase; letter-spacing:0.5px; font-size:8pt; }}
table.kn-table td {{ padding:7px 10px; border-bottom:1px solid var(--border); }}
table.kn-table tr:last-child td {{ border-bottom:none; }}
table.kn-table tfoot td {{ font-weight:700; color:var(--heading);
  border-top:2px solid var(--border); }}
.kn-r {{ text-align:right; }} .kn-c {{ text-align:center; }} .kn-l {{ text-align:left; }}
.kn-chart {{ margin:14px 0; background:var(--surface); border:1px solid var(--border);
  border-radius:10px; padding:10px; }}
.kn-callout {{ border-radius:10px; padding:12px 16px; margin:14px 0;
  border-left:4px solid var(--muted); background:var(--surface); }}
.kn-callout-title {{ font-weight:650; color:var(--heading); margin-bottom:3px; }}
.kn-callout.info {{ border-left-color:var(--info); }}
.kn-callout.success {{ border-left-color:var(--success); }}
.kn-callout.warning {{ border-left-color:var(--warning); }}
.kn-callout.danger {{ border-left-color:var(--danger); }}
.kn-callout.neutral {{ border-left-color:var(--muted); }}
.kn-badge {{ display:inline-block; padding:3px 10px; border-radius:999px;
  font-size:{ts.small}pt; font-weight:650; margin:6px 0; }}
.kn-badge.info {{ background:var(--info); color:#fff; }}
.kn-badge.success {{ background:var(--success); color:#fff; }}
.kn-badge.warning {{ background:var(--warning); color:#1b1b1b; }}
.kn-badge.danger {{ background:var(--danger); color:#fff; }}
.kn-badge.accent {{ background:var(--accent); color:#fff; }}
.kn-badge.neutral {{ background:var(--surface-alt); color:var(--text); }}
.kn-divider {{ border:none; border-top:1px solid var(--border); margin:20px 0; }}
.kn-footer {{ margin-top:40px; padding-top:16px; border-top:1px solid var(--border);
  color:var(--muted); font-size:{ts.small}pt; }}
""".strip()


def _kv(block: KeyValue, brand: Brand) -> str:
    cols = max(1, block.columns)
    cells = []
    for item in block.items:
        emph = " kn-emph" if item.emphasis else ""
        cells.append(
            f'<div class="kn-kv-cell{emph}">'
            f'<div class="kn-kv-stat">{_esc(item.stat.text())}</div>'
            f'<div class="kn-kv-label">{_esc(item.label)}</div></div>'
        )
    return (
        f'<div class="kn-kv" style="grid-template-columns:repeat({cols},1fr)">'
        + "".join(cells)
        + "</div>"
    )


def _table(block: Table) -> str:
    cls = {"left": "kn-l", "center": "kn-c", "right": "kn-r"}
    caption = f"<caption>{_esc(block.caption)}</caption>" if block.caption else ""
    head = "".join(
        f'<th class="{cls[c.alignment().value]}">{_esc(c.label)}</th>' for c in block.columns
    )
    body_rows = []
    for row in block.rows:
        tds = "".join(
            f'<td class="{cls[c.alignment().value]}">{_esc(row.get(c.key, ""))}</td>'
            for c in block.columns
        )
        body_rows.append(f"<tr>{tds}</tr>")
    foot = ""
    if block.total_row is not None:
        tds = "".join(
            f'<td class="{cls[c.alignment().value]}">{_esc(block.total_row.get(c.key, ""))}</td>'
            for c in block.columns
        )
        foot = f"<tfoot><tr>{tds}</tr></tfoot>"
    return (
        '<table class="kn-table">'
        f"{caption}<thead><tr>{head}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>{foot}</table>"
    )


def _block_html(block: Block, brand: Brand) -> str:
    if isinstance(block, Heading):
        tag = {1: "h2", 2: "h3", 3: "h4", 4: "h5"}.get(block.level, "h3")
        cls = "kn-section-title" if block.level == 1 else "kn-h"
        return f'<{tag} class="{cls}">{_esc(block.text)}</{tag}>'
    if isinstance(block, Paragraph):
        muted = " kn-muted" if block.muted else ""
        return f'<p class="kn-p{muted}">{_esc(block.text)}</p>'
    if isinstance(block, KeyValue):
        return _kv(block, brand)
    if isinstance(block, Table):
        return _table(block)
    if isinstance(block, Chart):
        return f'<div class="kn-chart">{render_chart(block, brand)}</div>'
    if isinstance(block, Callout):
        title = (
            f'<div class="kn-callout-title">{_esc(block.title)}</div>' if block.title else ""
        )
        return (
            f'<div class="kn-callout {block.tone.value}">{title}'
            f"<div>{_esc(block.text)}</div></div>"
        )
    if isinstance(block, Badge):
        return f'<div><span class="kn-badge {block.tone.value}">{_esc(block.text)}</span></div>'
    if isinstance(block, Divider):
        return '<hr class="kn-divider"/>'
    if isinstance(block, Spacer):
        return f'<div style="height:{block.size}px"></div>'
    return ""  # pragma: no cover - exhaustive above


def _section_html(section: Section, brand: Brand) -> str:
    title = (
        f'<h2 class="kn-section-title">{_esc(section.title)}</h2>' if section.title else ""
    )
    blocks = "".join(_block_html(b, brand) for b in section.blocks)
    return f'<section class="kn-section">{title}{blocks}</section>'


def render_html(report: Report, brand: Brand) -> str:
    """Render a report to a self-contained HTML document string."""
    meta = report.meta
    mark = (
        f'<div class="kn-mark">{brand.logo_svg}</div>' if brand.logo_svg else ""
    )
    subtitle = f'<p class="kn-subtitle">{_esc(meta.subtitle)}</p>' if meta.subtitle else ""
    cover = (
        '<header class="kn-cover">'
        f"{mark}"
        "<div>"
        f'<p class="kn-brandline">{_esc(brand.name)} · {_esc(brand.tagline)}</p>'
        f'<h1 class="kn-title">{_esc(meta.title)}</h1>'
        f"{subtitle}"
        "</div></header>"
    )
    body = "".join(_section_html(s, brand) for s in report.sections)
    footer_text = meta.footer or f"{brand.name} — {brand.tagline}"
    footer = f'<footer class="kn-footer">{_esc(footer_text)}</footer>'
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f"<title>{_esc(meta.title)}</title>"
        f"<style>{_css(brand)}</style></head>"
        f'<body><main class="kn-doc">{cover}{body}{footer}</main></body></html>'
    )


__all__ = ["render_html"]
