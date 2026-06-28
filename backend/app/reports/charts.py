"""A pure-Python SVG chart renderer — no new dependency.

Turns a :class:`~app.reports.model.Chart` spec into a deterministic SVG string.
The same SVG embeds directly in the HTML report and is rasterised into the PDF
(PyMuPDF's ``Page.insert_image`` accepts SVG bytes via an intermediate
``fitz.open("svg", ...)`` → PNG; see :mod:`app.reports.render.pdf`).

Determinism is a hard requirement (the golden-file tests pin the exact SVG): no
randomness, fixed coordinate rounding (:func:`_n`), and a stable color cycle from
the brand palette. Every numeric coordinate is rounded to 2 decimals so floating
arithmetic on different machines can't perturb the bytes.

Supported families (``ChartKind``): ``bar``, ``grouped_bar``, ``line``,
``area``, ``pie``, ``donut``, ``sparkline``, ``progress``.
"""

from __future__ import annotations

import html
import math

from app.reports.model import Chart, ChartKind, Series
from app.reports.theme import Brand

#: Default canvas width (matches the report content column).
_WIDTH = 640
#: Inner plot margins (top/right/bottom/left).
_MARGIN = (16, 16, 30, 44)


def _n(x: float) -> str:
    """Format a coordinate deterministically (2dp, no trailing-zero churn)."""
    # Round first, then strip an integral ".0" so 12.0 -> "12" stably.
    r = round(x + 0.0, 2)
    if r == int(r):
        return str(int(r))
    return f"{r:.2f}".rstrip("0").rstrip(".")


def _esc(text: str) -> str:
    """XML-escape text for SVG ``<text>`` content / attributes."""
    return html.escape(text, quote=True)


def _nice_max(value: float) -> float:
    """Round an axis maximum up to a clean 1/2/5×10ⁿ step (stable)."""
    if value <= 0:
        return 1.0
    exp = math.floor(math.log10(value))
    base = 10.0**exp
    frac = value / base
    if frac <= 1.0:
        nice = 1.0
    elif frac <= 2.0:
        nice = 2.0
    elif frac <= 5.0:
        nice = 5.0
    else:
        nice = 10.0
    return nice * base


def render_chart(chart: Chart, brand: Brand, *, width: int = _WIDTH) -> str:
    """Render a chart spec to a standalone SVG string."""
    height = chart.height
    dispatch = {
        ChartKind.BAR: _bar,
        ChartKind.GROUPED_BAR: _grouped_bar,
        ChartKind.LINE: _line,
        ChartKind.AREA: _line,  # area == line with fill
        ChartKind.PIE: _pie,
        ChartKind.DONUT: _pie,  # donut == pie with a hole
        ChartKind.SPARKLINE: _sparkline,
        ChartKind.PROGRESS: _progress,
    }
    body = dispatch[chart.kind](chart, brand, width, height)
    title = ""
    if chart.title:
        title = (
            f'<text x="{_n(width / 2)}" y="13" text-anchor="middle" '
            f'fill="{brand.palette.heading}" font-size="11" '
            f'font-family="{_esc(brand.font_family)}" font-weight="600">'
            f"{_esc(chart.title)}</text>"
        )
        # Push the plot down a touch when titled.
    return (
        f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        f"{title}{body}</svg>"
    )


# --------------------------------------------------------------------------- #
# Axis + grid helpers
# --------------------------------------------------------------------------- #


def _plot_box(width: int, height: int, *, titled: bool) -> tuple[float, float, float, float]:
    """Return ``(x0, y0, x1, y1)`` of the inner plot rectangle."""
    top, right, bottom, left = _MARGIN
    if titled:
        top += 14
    return (left, top, width - right, height - bottom)


def _axis(
    brand: Brand,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    vmax: float,
    *,
    ticks: int = 4,
) -> str:
    """A faint horizontal grid + y-axis value labels."""
    pal = brand.palette
    parts = [
        f'<line x1="{_n(x0)}" y1="{_n(y1)}" x2="{_n(x1)}" y2="{_n(y1)}" '
        f'stroke="{pal.border}" stroke-width="1"/>'
    ]
    for i in range(ticks + 1):
        frac = i / ticks
        y = y1 - (y1 - y0) * frac
        val = vmax * frac
        if i > 0:
            parts.append(
                f'<line x1="{_n(x0)}" y1="{_n(y)}" x2="{_n(x1)}" y2="{_n(y)}" '
                f'stroke="{pal.border}" stroke-width="0.6" stroke-opacity="0.5"/>'
            )
        parts.append(
            f'<text x="{_n(x0 - 6)}" y="{_n(y + 3)}" text-anchor="end" '
            f'fill="{pal.text_muted}" font-size="8" '
            f'font-family="{_esc(brand.font_family)}">{_esc(_fmt_axis(val))}</text>'
        )
    return "".join(parts)


def _fmt_axis(v: float) -> str:
    """Compact axis label (drops a redundant ``.0``)."""
    if v == int(v):
        return str(int(v))
    return f"{v:.1f}"


def _x_labels(brand: Brand, labels: tuple[str, ...], x0: float, x1: float, y: float) -> str:
    """Evenly spaced category labels beneath the axis."""
    if not labels:
        return ""
    n = len(labels)
    span = x1 - x0
    parts = []
    for i, lab in enumerate(labels):
        cx = x0 + span * ((i + 0.5) / n)
        parts.append(
            f'<text x="{_n(cx)}" y="{_n(y)}" text-anchor="middle" '
            f'fill="{brand.palette.text_muted}" font-size="8" '
            f'font-family="{_esc(brand.font_family)}">{_esc(lab)}</text>'
        )
    return "".join(parts)


def _series_max(series: tuple[Series, ...]) -> float:
    """The nice axis maximum over every value in every series."""
    vals = [v for s in series for v in s.values]
    return _nice_max(max(vals)) if vals else 1.0


# --------------------------------------------------------------------------- #
# Bar
# --------------------------------------------------------------------------- #


def _bar(chart: Chart, brand: Brand, width: int, height: int) -> str:
    if not chart.series:
        return ""
    series = chart.series[0]
    x0, y0, x1, y1 = _plot_box(width, height, titled=bool(chart.title))
    vmax = _nice_max(max(series.values)) if series.values else 1.0
    n = len(series.values)
    parts = [_axis(brand, x0, y0, x1, y1, vmax)]
    if n:
        span = x1 - x0
        slot = span / n
        bar_w = slot * 0.62
        for i, v in enumerate(series.values):
            bh = (y1 - y0) * (v / vmax) if vmax else 0.0
            bx = x0 + slot * i + (slot - bar_w) / 2
            by = y1 - bh
            color = series.color or brand.palette.series_color(i)
            parts.append(
                f'<rect x="{_n(bx)}" y="{_n(by)}" width="{_n(bar_w)}" '
                f'height="{_n(bh)}" rx="2" fill="{color}"/>'
            )
    parts.append(_x_labels(brand, chart.labels, x0, x1, y1 + 12))
    return "".join(parts)


def _grouped_bar(chart: Chart, brand: Brand, width: int, height: int) -> str:
    if not chart.series:
        return ""
    x0, y0, x1, y1 = _plot_box(width, height, titled=bool(chart.title))
    vmax = _series_max(chart.series)
    groups = max((len(s.values) for s in chart.series), default=0)
    g = len(chart.series)
    parts = [_axis(brand, x0, y0, x1, y1, vmax)]
    if groups and g:
        span = x1 - x0
        slot = span / groups
        group_w = slot * 0.72
        bar_w = group_w / g
        for gi in range(groups):
            for si, s in enumerate(chart.series):
                if gi >= len(s.values):
                    continue
                v = s.values[gi]
                bh = (y1 - y0) * (v / vmax) if vmax else 0.0
                bx = x0 + slot * gi + (slot - group_w) / 2 + bar_w * si
                color = s.color or brand.palette.series_color(si)
                parts.append(
                    f'<rect x="{_n(bx)}" y="{_n(y1 - bh)}" width="{_n(bar_w)}" '
                    f'height="{_n(bh)}" rx="1.5" fill="{color}"/>'
                )
    parts.append(_x_labels(brand, chart.labels, x0, x1, y1 + 12))
    parts.append(_legend(chart.series, brand, x0, y0 - 2))
    return "".join(parts)


def _legend(series: tuple[Series, ...], brand: Brand, x: float, y: float) -> str:
    """A compact inline legend across the top of the plot."""
    parts = []
    cx = x
    for i, s in enumerate(series):
        color = s.color or brand.palette.series_color(i)
        parts.append(
            f'<rect x="{_n(cx)}" y="{_n(y - 7)}" width="8" height="8" rx="1.5" fill="{color}"/>'
        )
        parts.append(
            f'<text x="{_n(cx + 11)}" y="{_n(y)}" fill="{brand.palette.text_muted}" '
            f'font-size="8" font-family="{_esc(brand.font_family)}">{_esc(s.name)}</text>'
        )
        cx += 13 + len(s.name) * 5.0
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Line / area
# --------------------------------------------------------------------------- #


def _line(chart: Chart, brand: Brand, width: int, height: int) -> str:
    if not chart.series:
        return ""
    x0, y0, x1, y1 = _plot_box(width, height, titled=bool(chart.title))
    vmax = _series_max(chart.series)
    parts = [_axis(brand, x0, y0, x1, y1, vmax)]
    fill = chart.kind == ChartKind.AREA
    for si, s in enumerate(chart.series):
        pts = _points(s.values, x0, x1, y0, y1, vmax)
        if not pts:
            continue
        color = s.color or brand.palette.series_color(si)
        path = "M " + " L ".join(f"{_n(px)} {_n(py)}" for px, py in pts)
        if fill:
            area = (
                path
                + f" L {_n(pts[-1][0])} {_n(y1)} L {_n(pts[0][0])} {_n(y1)} Z"
            )
            parts.append(f'<path d="{area}" fill="{color}" fill-opacity="0.18"/>')
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" '
            'stroke-linejoin="round" stroke-linecap="round"/>'
        )
        for px, py in pts:
            parts.append(f'<circle cx="{_n(px)}" cy="{_n(py)}" r="2.2" fill="{color}"/>')
    parts.append(_x_labels(brand, chart.labels, x0, x1, y1 + 12))
    if len(chart.series) > 1:
        parts.append(_legend(chart.series, brand, x0, y0 - 2))
    return "".join(parts)


def _points(
    values: tuple[float, ...], x0: float, x1: float, y0: float, y1: float, vmax: float
) -> list[tuple[float, float]]:
    """Map a value series to plot coordinates (left→right, top is high)."""
    n = len(values)
    if n == 0:
        return []
    if n == 1:
        cx = (x0 + x1) / 2
        cy = y1 - (y1 - y0) * (values[0] / vmax if vmax else 0.0)
        return [(cx, cy)]
    span = x1 - x0
    out = []
    for i, v in enumerate(values):
        px = x0 + span * (i / (n - 1))
        py = y1 - (y1 - y0) * (v / vmax if vmax else 0.0)
        out.append((px, py))
    return out


# --------------------------------------------------------------------------- #
# Pie / donut
# --------------------------------------------------------------------------- #


def _pie(chart: Chart, brand: Brand, width: int, height: int) -> str:
    values = [s.values[0] for s in chart.series if s.values]
    names = [s.name for s in chart.series if s.values]
    total = sum(values)
    cx = width / 2
    cy = height / 2 + (7 if chart.title else 0)
    radius = min(width, height) / 2 - 18
    if total <= 0:
        return (
            f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(radius)}" '
            f'fill="none" stroke="{brand.palette.border}" stroke-width="1.5"/>'
        )
    inner = radius * 0.58 if chart.kind == ChartKind.DONUT else 0.0
    parts = []
    angle = -math.pi / 2  # start at 12 o'clock
    for i, v in enumerate(values):
        frac = v / total
        sweep = frac * 2 * math.pi
        color = chart.series[i].color or brand.palette.series_color(i)
        parts.append(_arc_path(cx, cy, radius, inner, angle, angle + sweep, color))
        angle += sweep
    # Legend on the right.
    ly = cy - radius
    for i, name in enumerate(names):
        color = chart.series[i].color or brand.palette.series_color(i)
        pct = values[i] / total * 100
        parts.append(
            f'<rect x="{_n(cx + radius + 8)}" y="{_n(ly + i * 15)}" width="9" '
            f'height="9" rx="2" fill="{color}"/>'
            f'<text x="{_n(cx + radius + 21)}" y="{_n(ly + i * 15 + 8)}" '
            f'fill="{brand.palette.text_muted}" font-size="8.5" '
            f'font-family="{_esc(brand.font_family)}">'
            f"{_esc(name)} · {_fmt_axis(pct)}%</text>"
        )
    return "".join(parts)


def _arc_path(
    cx: float, cy: float, r: float, inner: float, a0: float, a1: float, color: str
) -> str:
    """A pie wedge (or donut segment) as an SVG path."""
    large = 1 if (a1 - a0) > math.pi else 0
    x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
    x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
    if inner <= 0:
        d = (
            f"M {_n(cx)} {_n(cy)} L {_n(x0)} {_n(y0)} "
            f"A {_n(r)} {_n(r)} 0 {large} 1 {_n(x1)} {_n(y1)} Z"
        )
    else:
        ix0, iy0 = cx + inner * math.cos(a1), cy + inner * math.sin(a1)
        ix1, iy1 = cx + inner * math.cos(a0), cy + inner * math.sin(a0)
        d = (
            f"M {_n(x0)} {_n(y0)} A {_n(r)} {_n(r)} 0 {large} 1 {_n(x1)} {_n(y1)} "
            f"L {_n(ix0)} {_n(iy0)} A {_n(inner)} {_n(inner)} 0 {large} 0 "
            f"{_n(ix1)} {_n(iy1)} Z"
        )
    return f'<path d="{d}" fill="{color}"/>'


# --------------------------------------------------------------------------- #
# Sparkline (axis-less inline trend)
# --------------------------------------------------------------------------- #


def _sparkline(chart: Chart, brand: Brand, width: int, height: int) -> str:
    if not chart.series or not chart.series[0].values:
        return ""
    s = chart.series[0]
    pad = 4
    vmax = max(s.values)
    vmin = min(s.values)
    rng = (vmax - vmin) or 1.0
    n = len(s.values)
    span = width - 2 * pad
    pts = []
    for i, v in enumerate(s.values):
        px = pad + (span * (i / (n - 1)) if n > 1 else span / 2)
        py = (height - pad) - (height - 2 * pad) * ((v - vmin) / rng)
        pts.append((px, py))
    color = s.color or brand.palette.accent
    path = "M " + " L ".join(f"{_n(px)} {_n(py)}" for px, py in pts)
    last = pts[-1]
    return (
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.8" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        f'<circle cx="{_n(last[0])}" cy="{_n(last[1])}" r="2.4" fill="{color}"/>'
    )


# --------------------------------------------------------------------------- #
# Progress (a single fraction as a rounded bar)
# --------------------------------------------------------------------------- #


def _progress(chart: Chart, brand: Brand, width: int, height: int) -> str:
    frac = 0.0
    if chart.series and chart.series[0].values:
        frac = max(0.0, min(1.0, chart.series[0].values[0]))
    pal = brand.palette
    track_h = min(height - 8, 18)
    y = (height - track_h) / 2
    pad = 8
    track_w = width - 2 * pad
    fill_w = track_w * frac
    explicit = chart.series[0].color if chart.series and chart.series[0].color else None
    color = explicit or pal.accent
    label = chart.options.get("label", f"{round(frac * 100)}%")
    return (
        f'<rect x="{_n(pad)}" y="{_n(y)}" width="{_n(track_w)}" height="{_n(track_h)}" '
        f'rx="{_n(track_h / 2)}" fill="{pal.surface_alt}"/>'
        f'<rect x="{_n(pad)}" y="{_n(y)}" width="{_n(fill_w)}" height="{_n(track_h)}" '
        f'rx="{_n(track_h / 2)}" fill="{color}"/>'
        f'<text x="{_n(width / 2)}" y="{_n(y + track_h - 4)}" text-anchor="middle" '
        f'fill="{pal.heading}" font-size="9" font-weight="600" '
        f'font-family="{_esc(brand.font_family)}">{_esc(str(label))}</text>'
    )


__all__ = ["render_chart"]
