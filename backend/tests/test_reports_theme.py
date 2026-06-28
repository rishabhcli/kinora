"""Unit tests for the report theme/branding layer."""

from __future__ import annotations

import pytest

from app.reports.theme import (
    Brand,
    Palette,
    TypeScale,
    certificate_brand,
    default_brand,
    hex_to_rgb,
    mix,
)


def test_hex_to_rgb_parses_6_and_3_digit() -> None:
    assert hex_to_rgb("#ffffff") == (1.0, 1.0, 1.0)
    assert hex_to_rgb("000000") == (0.0, 0.0, 0.0)
    r, g, b = hex_to_rgb("#f00")
    assert (round(r), round(g), round(b)) == (1, 0, 0)


def test_hex_to_rgb_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="not a hex color"):
        hex_to_rgb("#12")
    with pytest.raises(ValueError, match="not a hex color"):
        hex_to_rgb("#gggggg")


def test_mix_blends_endpoints() -> None:
    assert mix("#000000", "#ffffff", 0.0) == "#000000"
    assert mix("#000000", "#ffffff", 1.0) == "#ffffff"
    assert mix("#000000", "#ffffff", 0.5) == "#808080"


def test_palette_series_color_cycles() -> None:
    pal = Palette()
    n = len(pal.series)
    assert pal.series_color(0) == pal.series_color(n)
    assert pal.series_color(1) == pal.series[1]


def test_palette_tone_resolves_names() -> None:
    pal = Palette()
    assert pal.tone("success") == pal.success
    assert pal.tone("danger") == pal.danger
    assert pal.tone("unknown") == pal.text_muted


def test_type_scale_heading_size_maps_levels() -> None:
    ts = TypeScale()
    assert ts.heading_size(1) == ts.h1
    assert ts.heading_size(4) == ts.h4
    assert ts.heading_size(99) == ts.h2  # fallback


def test_default_and_certificate_brands_differ() -> None:
    house = default_brand()
    cert = certificate_brand()
    assert isinstance(house, Brand)
    assert house.logo_svg is not None
    # Certificate is the light palette (white background).
    assert cert.palette.background == "#ffffff"
    assert house.palette.background != "#ffffff"


def test_brand_to_dict_is_serializable() -> None:
    d = default_brand().to_dict()
    assert d["name"] == "Kinora"
    assert "palette" in d and "type_scale" in d
