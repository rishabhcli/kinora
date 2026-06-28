"""Tests for HLS/DASH packaging — pure planning + real ffmpeg segmenting."""

from __future__ import annotations

import pytest

from app.media.errors import PackagingError
from app.media.packaging import (
    DASH_MANIFEST_NAME,
    MASTER_PLAYLIST_NAME,
    VariantSpec,
    abr_ladder,
    master_playlist,
)
from app.media.testing import media_available, tiny_mp4

# -- pure planning (no ffmpeg) ---------------------------------------------- #


def test_variant_width_is_even_and_aspect_preserving() -> None:
    v = VariantSpec("640p", 640, 800)
    # 720x1280 source → width at height 640 = 360 (even)
    assert v.width_for(720, 1280) == 360
    assert v.resolution_for(720, 1280) == "360x640"


def test_variant_width_rounds_to_even() -> None:
    v = VariantSpec("641p", 641, 800)
    w = v.width_for(720, 1280)
    assert w % 2 == 0


def test_variant_bandwidth_has_headroom() -> None:
    v = VariantSpec("x", 100, 1000)
    assert v.bandwidth_bps == int(1000 * 1000 * 1.1)


def test_abr_ladder_full_for_large_source() -> None:
    ladder = abr_ladder(1280)
    names = [v.name for v in ladder]
    assert names == ["1280p", "854p", "640p"]


def test_abr_ladder_drops_rungs_above_source() -> None:
    ladder = abr_ladder(854)
    heights = [v.height for v in ladder]
    assert max(heights) <= 854
    assert all(h <= 854 for h in heights)


def test_abr_ladder_adds_native_top_rung() -> None:
    # source between rungs (e.g. 900) → add a native-height top rung
    ladder = abr_ladder(900)
    assert ladder[0].height == 900


def test_abr_ladder_small_source_single_rung() -> None:
    ladder = abr_ladder(240)
    assert len(ladder) == 1
    assert ladder[0].height == 240


def test_abr_ladder_none_is_full() -> None:
    assert [v.name for v in abr_ladder(None)] == ["1280p", "854p", "640p"]


def test_master_playlist_lists_variants() -> None:
    variants = [
        (VariantSpec("1280p", 1280, 2800), "1280p/index.m3u8"),
        (VariantSpec("640p", 640, 800), "640p/index.m3u8"),
    ]
    m = master_playlist(variants, src_w=720, src_h=1280)
    assert m.startswith("#EXTM3U\n")
    assert "#EXT-X-VERSION:6" in m
    assert "1280p/index.m3u8" in m
    assert "640p/index.m3u8" in m
    assert "RESOLUTION=720x1280" in m
    assert "RESOLUTION=360x640" in m
    assert "BANDWIDTH=" in m


# -- real ffmpeg packaging --------------------------------------------------- #

requires_ffmpeg = pytest.mark.skipif(
    not media_available(), reason="ffmpeg not available for packaging tests"
)


@pytest.fixture(scope="module")
def clip() -> bytes:
    return tiny_mp4(duration_s=2.0, width=180, height=320, fps=24)


@requires_ffmpeg
def test_package_hls_produces_master_and_segments(clip: bytes) -> None:
    from app.media.packaging import package_hls

    result = package_hls(clip, segment_s=1, variants=[VariantSpec("160p", 160, 400)])
    assert result.entrypoint == MASTER_PLAYLIST_NAME
    assert MASTER_PLAYLIST_NAME in result.files
    # the master references the variant playlist
    master = result.files[MASTER_PLAYLIST_NAME].decode()
    assert "160p/index.m3u8" in master
    assert "160p/index.m3u8" in result.files
    # at least one segment
    assert result.segment_count >= 1
    assert any(n.endswith(".ts") for n in result.files)


@requires_ffmpeg
def test_package_hls_default_ladder_from_probe(clip: bytes) -> None:
    from app.media.packaging import package_hls

    # 320-tall source → ladder caps at 320 (single native rung)
    result = package_hls(clip, segment_s=1)
    assert len(result.variants) >= 1
    # never a rung taller than the source
    master = result.files[MASTER_PLAYLIST_NAME].decode()
    assert "RESOLUTION=" in master


@requires_ffmpeg
def test_package_dash_produces_manifest(clip: bytes) -> None:
    from app.media.packaging import package_dash

    result = package_dash(clip, segment_s=1)
    assert result.entrypoint == DASH_MANIFEST_NAME
    assert DASH_MANIFEST_NAME in result.files
    manifest = result.files[DASH_MANIFEST_NAME].decode()
    assert "<MPD" in manifest


def test_package_hls_empty_raises() -> None:
    from app.media.packaging import package_hls

    with pytest.raises(PackagingError):
        package_hls(b"")
