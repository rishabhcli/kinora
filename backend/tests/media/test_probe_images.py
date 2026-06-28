"""Tests for media probe + poster/thumbnail/sprite generation (real ffmpeg).

These build a tiny *real* mp4 with the bundled ffmpeg and exercise the genuine
pipeline. They skip cleanly when no ffmpeg binary / Pillow is resolvable.
"""

from __future__ import annotations

import pytest

from app.media.kinds import MediaAssetKind
from app.media.testing import media_available, tiny_mp4, tiny_png

pytestmark = pytest.mark.skipif(
    not media_available(), reason="ffmpeg/Pillow not available for media derivation tests"
)


@pytest.fixture(scope="module")
def clip() -> bytes:
    # a 1s, 64x96 portrait test clip with audio
    return tiny_mp4(duration_s=1.0, width=64, height=96, fps=24)


def test_probe_media_reports_geometry(clip: bytes) -> None:
    from app.media.probe import probe_media

    probe = probe_media(clip)
    assert probe.has_video is True
    assert probe.width == 64
    assert probe.height == 96
    assert probe.is_portrait is True
    assert probe.is_film_geometry is False
    assert probe.duration_s > 0
    assert probe.aspect_ratio == pytest.approx(64 / 96)


def test_metadata_for_video_probes(clip: bytes) -> None:
    from app.media.probe import metadata_for

    meta = metadata_for(clip, storage_key="clips/b/s.mp4", kind=MediaAssetKind.CLIP)
    assert meta.content_type == "video/mp4"
    assert meta.width == 64
    assert meta.height == 96
    assert meta.duration_s is not None
    assert meta.size_bytes == len(clip)
    assert meta.content_hash is not None


def test_metadata_for_still_skips_probe() -> None:
    from app.media.probe import metadata_for

    png = tiny_png()
    meta = metadata_for(png, storage_key="posters/x.png", kind=MediaAssetKind.POSTER)
    assert meta.content_type == "image/png"
    # stills are not AV-probed → no duration
    assert meta.duration_s is None


def test_extract_poster_is_png(clip: bytes) -> None:
    from app.media.images import extract_poster, png_size

    poster = extract_poster(clip)
    assert poster[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = png_size(poster)
    assert (w, h) == (64, 96)


def test_extract_thumbnail_scales_width(clip: bytes) -> None:
    from app.media.images import extract_thumbnail, png_size

    thumb = extract_thumbnail(clip, width=32)
    w, h = png_size(thumb)
    assert w == 32
    assert h > 0  # height derived from aspect


def test_build_sprite_sheet_grid(clip: bytes) -> None:
    from app.media.images import build_sprite_sheet, png_size

    sheet = build_sprite_sheet(clip, count=4, columns=2, tile_width=40)
    assert sheet.columns == 2
    assert sheet.rows == 2
    assert sheet.tile_width == 40
    assert sheet.tile_height > 0
    assert sheet.tile_count == 4
    assert sheet.interval_s > 0
    # the sheet image is a real PNG of cols*tile x rows*tile
    w, h = png_size(sheet.image)
    assert w == sheet.sheet_width
    assert h == sheet.sheet_height


def test_sprite_sheet_default_square_grid(clip: bytes) -> None:
    from app.media.images import build_sprite_sheet

    sheet = build_sprite_sheet(clip, count=9, tile_width=20)
    # default near-square: ceil(sqrt(9)) = 3 columns
    assert sheet.columns == 3
    assert sheet.rows == 3


def test_sprite_vtt_matches_sheet(clip: bytes) -> None:
    from app.media.images import build_sprite_sheet
    from app.media.vtt import sprite_vtt

    sheet = build_sprite_sheet(clip, count=4, columns=2, tile_width=40)
    vtt = sprite_vtt(
        sprite_url="sprite.png",
        columns=sheet.columns,
        rows=sheet.rows,
        tile_width=sheet.tile_width,
        tile_height=sheet.tile_height,
        tile_count=sheet.tile_count,
        interval_s=sheet.interval_s,
    )
    assert vtt.count("#xywh=") == 4
    assert f"#xywh=0,0,{sheet.tile_width},{sheet.tile_height}" in vtt


def test_build_sprite_sheet_rejects_zero_count(clip: bytes) -> None:
    from app.media.errors import PackagingError
    from app.media.images import build_sprite_sheet

    with pytest.raises(PackagingError):
        build_sprite_sheet(clip, count=0)
