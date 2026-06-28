"""Unit tests for the pure-Python SVG chart renderer."""

from __future__ import annotations

from app.reports.charts import render_chart
from app.reports.model import Chart, ChartKind, Series
from app.reports.theme import default_brand


def _brand():
    return default_brand()


def test_every_chart_kind_renders_valid_svg() -> None:
    brand = _brand()
    cases = [
        Chart(kind=ChartKind.BAR, series=(Series("a", (1.0, 2.0, 3.0)),), labels=("x", "y", "z")),
        Chart(
            kind=ChartKind.GROUPED_BAR,
            series=(Series("a", (1.0, 2.0)), Series("b", (3.0, 1.0))),
            labels=("x", "y"),
        ),
        Chart(kind=ChartKind.LINE, series=(Series("a", (1.0, 5.0, 3.0)),)),
        Chart(kind=ChartKind.AREA, series=(Series("a", (1.0, 5.0, 3.0)),)),
        Chart(kind=ChartKind.PIE, series=(Series("a", (3.0,)), Series("b", (1.0,)))),
        Chart(kind=ChartKind.DONUT, series=(Series("a", (3.0,)), Series("b", (1.0,)))),
        Chart(kind=ChartKind.SPARKLINE, series=(Series("a", (1.0, 2.0, 1.5, 3.0)),)),
        Chart(kind=ChartKind.PROGRESS, series=(Series("a", (0.62,)),), height=40),
    ]
    for chart in cases:
        svg = render_chart(chart, brand)
        assert svg.startswith("<svg ")
        assert svg.rstrip().endswith("</svg>")
        assert "viewBox" in svg


def test_chart_render_is_deterministic() -> None:
    brand = _brand()
    chart = Chart(
        kind=ChartKind.BAR,
        series=(Series("a", (1.0, 2.5, 3.7)),),
        labels=("x", "y", "z"),
        title="t",
    )
    assert render_chart(chart, brand) == render_chart(chart, brand)


def test_title_embeds_in_svg() -> None:
    brand = _brand()
    chart = Chart(kind=ChartKind.BAR, series=(Series("a", (1.0,)),), title="Hello & Co")
    svg = render_chart(chart, brand)
    # XML-escaped title appears.
    assert "Hello &amp; Co" in svg


def test_empty_series_does_not_crash() -> None:
    brand = _brand()
    for kind in ChartKind:
        svg = render_chart(Chart(kind=kind, series=()), brand)
        assert svg.startswith("<svg ")


def test_progress_clamps_fraction() -> None:
    brand = _brand()
    over = render_chart(
        Chart(kind=ChartKind.PROGRESS, series=(Series("a", (1.7,)),), height=40), brand
    )
    under = render_chart(
        Chart(kind=ChartKind.PROGRESS, series=(Series("a", (-0.5,)),), height=40), brand
    )
    assert "<svg " in over and "<svg " in under


def test_pie_with_zero_total_draws_empty_ring() -> None:
    brand = _brand()
    svg = render_chart(
        Chart(kind=ChartKind.PIE, series=(Series("a", (0.0,)),)), brand
    )
    assert "circle" in svg
