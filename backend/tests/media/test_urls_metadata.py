"""Unit tests for URL signing contract + asset metadata helpers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.media.metadata import (
    DEFAULT_CONTENT_TYPE,
    AssetMetadata,
    guess_content_type,
    sniff_image_suffix,
    suffix_for,
)
from app.media.testing import FakeMediaStore
from app.media.urls import (
    DEFAULT_TTL_S,
    MAX_TTL_S,
    MIN_TTL_S,
    clamp_ttl,
    media_url,
    rewrite_for_browser,
)

# -- urls -------------------------------------------------------------------- #


def test_clamp_ttl_band() -> None:
    assert clamp_ttl(0) == MIN_TTL_S
    assert clamp_ttl(10**9) == MAX_TTL_S
    assert clamp_ttl(120) == 120


def test_rewrite_minio_to_localhost_idempotent() -> None:
    assert rewrite_for_browser("http://minio:9000/kinora/x") == "http://localhost:9000/kinora/x"
    # already-correct / CDN urls are untouched
    url = "https://cdn.example.com/kinora/x"
    assert rewrite_for_browser(url) == url
    assert rewrite_for_browser("http://localhost:9000/x") == "http://localhost:9000/x"


def test_media_url_prefers_public_and_rewrites() -> None:
    store = FakeMediaStore(public_base_url="http://minio:9000/kinora")
    url = media_url(store, "clips/b/s.mp4")
    assert url == "http://localhost:9000/kinora/clips/b/s.mp4"


def test_media_url_falls_back_to_signed() -> None:
    store = FakeMediaStore()  # no public base
    url = media_url(store, "clips/b/s.mp4", ttl=DEFAULT_TTL_S)
    assert url.startswith("https://signed.invalid/clips/b/s.mp4")
    assert f"ttl={DEFAULT_TTL_S}" in url


def test_media_url_clamps_signed_ttl() -> None:
    store = FakeMediaStore()
    url = media_url(store, "k", ttl=5)  # below MIN_TTL_S
    assert f"ttl={MIN_TTL_S}" in url


# -- metadata ---------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a/b/clip.mp4", "video/mp4"),
        ("poster.PNG", "image/png"),
        ("still.jpeg", "image/jpeg"),
        ("voice.wav", "audio/wav"),
        ("doc.pdf", "application/pdf"),
        ("playlist.m3u8", "application/vnd.apple.mpegurl"),
        ("seg.ts", "video/mp2t"),
        ("cues.vtt", "text/vtt"),
        ("noext", DEFAULT_CONTENT_TYPE),
        ("weird.xyz", DEFAULT_CONTENT_TYPE),
    ],
)
def test_guess_content_type(name: str, expected: str) -> None:
    assert guess_content_type(name) == expected


def test_suffix_for_roundtrips() -> None:
    assert suffix_for("video/mp4") == ".mp4"
    assert suffix_for("image/png") == ".png"
    assert suffix_for("text/vtt") == ".vtt"
    assert suffix_for("application/x-unknown") == ""
    # parameters are tolerated
    assert suffix_for("text/vtt; charset=utf-8") == ".vtt"


def test_sniff_image_suffix() -> None:
    assert sniff_image_suffix(b"\x89PNG\r\n\x1a\n....") == ".png"
    assert sniff_image_suffix(b"\xff\xd8\xff\xe0") == ".jpg"
    assert sniff_image_suffix(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP") == ".webp"
    assert sniff_image_suffix(b"GIF89a") == ".gif"


def test_asset_metadata_derived_props() -> None:
    m = AssetMetadata(
        storage_key="k",
        content_type="video/mp4",
        width=720,
        height=1280,
        duration_s=5.0,
    )
    assert m.is_visual is True
    assert m.is_timed is True
    assert m.aspect_ratio == pytest.approx(720 / 1280)

    audio = AssetMetadata(storage_key="a", content_type="audio/wav", duration_s=3.0)
    assert audio.is_visual is False
    assert audio.is_timed is True
    assert audio.aspect_ratio is None


def test_asset_metadata_with_meta_merges() -> None:
    m = AssetMetadata(storage_key="k", meta={"a": 1})
    m2 = m.with_meta(b=2)
    assert m2.meta == {"a": 1, "b": 2}
    # original is frozen / untouched
    assert m.meta == {"a": 1}


def test_asset_metadata_is_frozen() -> None:
    m = AssetMetadata(storage_key="k")
    with pytest.raises(ValidationError):
        m.storage_key = "other"  # type: ignore[misc]
