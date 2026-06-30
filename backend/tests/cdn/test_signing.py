"""Signed-URL generation: expiry, range-friendliness, provider abstraction."""

from __future__ import annotations

import pytest

from app.cdn.signing import RANGE_FRIENDLY_METHODS, SignedUrl, sign_url
from app.cdn.testing import FakeRegionStore
from app.media.urls import MAX_TTL_S, MIN_TTL_S

KEY = "clips/book1/shot_00001.mp4"


class _FakeEdge:
    def __init__(self, region_id: str) -> None:
        self._region_id = region_id

    @property
    def region_id(self) -> str:
        return self._region_id

    def edge_url(self, key: str, *, ttl: int) -> str:
        return f"https://{self._region_id}.edge.example.com/{key}?token=abc&exp={ttl}"


def test_presign_records_expiry_and_clamps_ttl_floor() -> None:
    store = FakeRegionStore("eu")
    signed = sign_url(store, KEY, now=1000.0, ttl=1)  # below the 60s floor
    assert signed.region_id == "eu"
    assert signed.expires_at == pytest.approx(1000.0 + MIN_TTL_S)
    assert signed.range_supported is True


def test_presign_clamps_ttl_ceiling() -> None:
    store = FakeRegionStore("eu")
    signed = sign_url(store, KEY, now=0.0, ttl=10**9)  # above the 7-day ceiling
    assert signed.expires_at == pytest.approx(MAX_TTL_S)


def test_public_base_has_no_expiry() -> None:
    store = FakeRegionStore("eu", public_base_url="https://cdn.example.com")
    signed = sign_url(store, KEY, now=1000.0, ttl=3600)
    assert signed.expires_at is None
    assert signed.url == f"https://cdn.example.com/{KEY}"


def test_edge_token_preferred_over_origin_presign() -> None:
    store = FakeRegionStore("eu")  # no public base -> would presign
    signed = sign_url(store, KEY, now=500.0, ttl=300, edge=_FakeEdge("eu"))
    assert "edge.example.com" in signed.url
    assert signed.expires_at == pytest.approx(500.0 + 300)


def test_minio_authority_rewritten_for_browser() -> None:
    store = FakeRegionStore("eu", public_base_url="http://minio:9000/kinora")
    signed = sign_url(store, KEY, now=0.0)
    assert signed.url.startswith("http://localhost:9000/kinora/")


def test_is_expired_respects_skew_and_public() -> None:
    expiring = SignedUrl(url="u", key=KEY, region_id="eu", expires_at=100.0)
    assert not expiring.is_expired(now=90.0)
    assert expiring.is_expired(now=100.0)
    # With skew, it is considered expired a little early.
    assert expiring.is_expired(now=95.0, skew_s=10.0)
    # A public URL (no expiry) never expires.
    public = SignedUrl(url="u", key=KEY, region_id="eu", expires_at=None)
    assert not public.is_expired(now=10**12)


def test_range_friendly_methods_contract() -> None:
    assert RANGE_FRIENDLY_METHODS == ("GET", "HEAD")
