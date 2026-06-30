"""Rendition-ladder construction + ABR selection (pure, no ffmpeg)."""

from __future__ import annotations

import pytest

from app.delivery.errors import LadderError
from app.delivery.ladder import (
    Rendition,
    build_ladder,
    scale_to_short_edge,
    select_rendition,
    sort_ladder,
    validate_ladder,
)


def test_scale_to_short_edge_preserves_aspect_and_is_even() -> None:
    # 720x1280 vertical, scale to a 360 short edge → 360x640, both even.
    w, h = scale_to_short_edge(720, 1280, 360)
    assert (w, h) == (360, 640)
    # Odd-rounding source is bumped up to even on both dims.
    w2, h2 = scale_to_short_edge(721, 1281, 241)
    assert w2 % 2 == 0 and h2 % 2 == 0


def test_build_ladder_clamps_to_master_no_upscale() -> None:
    # A 720-wide master must not offer a rung above 720 short edge.
    ladder = build_ladder(source_width=720, source_height=1280)
    shorts = [r.short_edge for r in ladder]
    assert max(shorts) <= 720
    assert shorts == sorted(shorts, reverse=True)  # descending by bandwidth ≈ res
    # Default rungs 720/540/360/240 all survive against a 720 master.
    assert {r.name for r in ladder} == {"720p", "540p", "360p", "240p"}


def test_build_ladder_drops_rungs_taller_than_source() -> None:
    # A 360-wide master drops 720p and 540p.
    ladder = build_ladder(source_width=360, source_height=640)
    names = {r.name for r in ladder}
    assert "720p" not in names and "540p" not in names
    assert "360p" in names and "240p" in names


def test_build_ladder_tiny_source_yields_single_rung() -> None:
    ladder = build_ladder(source_width=120, source_height=200)
    assert len(ladder) == 1
    assert ladder[0].short_edge == 120


def test_build_ladder_dedupes_resolutions() -> None:
    # Two rungs that scale to the same resolution must not both appear.
    rungs = [("a", 300, 800, 96), ("b", 300, 700, 96)]
    ladder = build_ladder(source_width=300, source_height=500, rungs=rungs)
    resolutions = [r.resolution for r in ladder]
    assert len(resolutions) == len(set(resolutions))


def test_build_ladder_rejects_bad_geometry() -> None:
    with pytest.raises(LadderError):
        build_ladder(source_width=0, source_height=100)
    with pytest.raises(LadderError):
        build_ladder(source_width=100, source_height=100, fps=0)


def test_rendition_bandwidth_and_codec_fields() -> None:
    r = Rendition(
        name="720p", width=720, height=1280, video_bitrate_kbps=2800, max_bitrate_kbps=4200
    )
    assert r.total_bitrate_kbps == 2800 + 128
    assert r.average_bandwidth_bps == (2800 + 128) * 1000
    assert r.peak_bandwidth_bps == (4200 + 128) * 1000
    assert r.resolution == "720x1280"
    assert r.rfc6381_codecs == "avc1.640028,mp4a.40.2"


def test_sort_ladder_is_deterministic_highest_first() -> None:
    ladder = build_ladder(source_width=720, source_height=1280)
    shuffled = list(reversed(ladder))
    assert sort_ladder(shuffled) == sort_ladder(ladder)
    assert sort_ladder(ladder)[0].peak_bandwidth_bps >= sort_ladder(ladder)[-1].peak_bandwidth_bps


def test_select_rendition_picks_richest_that_fits() -> None:
    ladder = build_ladder(source_width=720, source_height=1280)
    top = sort_ladder(ladder)[0]
    # Abundant bandwidth → top rung.
    assert select_rendition(ladder, available_bps=50_000_000) == top
    # Just under the 360p peak (with headroom) → 360p or below, never 720p.
    chosen = select_rendition(ladder, available_bps=1_400_000)
    assert chosen.peak_bandwidth_bps <= 1_400_000 * 0.85 or chosen == sort_ladder(ladder)[-1]


def test_select_rendition_below_floor_returns_lowest() -> None:
    ladder = build_ladder(source_width=720, source_height=1280)
    lowest = sort_ladder(ladder)[-1]
    assert select_rendition(ladder, available_bps=1) == lowest


def test_select_rendition_empty_ladder_raises() -> None:
    with pytest.raises(LadderError):
        select_rendition([], available_bps=1000)


def test_validate_ladder_rejects_empty_and_dupes() -> None:
    with pytest.raises(LadderError):
        validate_ladder([])
    r = Rendition(name="a", width=360, height=640, video_bitrate_kbps=900, max_bitrate_kbps=1300)
    r2 = Rendition(name="b", width=360, height=640, video_bitrate_kbps=800, max_bitrate_kbps=1200)
    with pytest.raises(LadderError):
        validate_ladder([r, r2])  # duplicate 360x640
