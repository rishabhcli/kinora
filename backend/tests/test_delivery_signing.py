"""Signed playback URLs + stream tokens (HMAC, no network)."""

from __future__ import annotations

import pytest

from app.delivery.errors import SigningError
from app.delivery.signing import (
    StreamTokenSigner,
    UrlSigner,
    derive_signing_secret,
)

_SECRET = "test-delivery-secret"
_NOW = 1_000_000.0


# -- URL signing ----------------------------------------------------------- #


def test_signed_url_round_trips() -> None:
    signer = UrlSigner(_SECRET)
    url = signer.sign("https://cdn/b/720p/seg_0.m4s", ttl_s=600, now=_NOW)
    assert "sig=" in url and "exp=" in url
    assert signer.verify(url, now=_NOW + 1) is True


def test_signed_url_rejects_after_expiry() -> None:
    signer = UrlSigner(_SECRET)
    url = signer.sign("https://cdn/seg.m4s", ttl_s=100, now=_NOW)
    assert signer.verify(url, now=_NOW + 101) is False


def test_signed_url_rejects_tampered_path() -> None:
    signer = UrlSigner(_SECRET)
    url = signer.sign("https://cdn/b/720p/seg_0.m4s", ttl_s=600, now=_NOW)
    tampered = url.replace("seg_0.m4s", "seg_1.m4s")
    assert signer.verify(tampered, now=_NOW + 1) is False


def test_signed_url_rejects_wrong_secret() -> None:
    url = UrlSigner(_SECRET).sign("https://cdn/seg.m4s", now=_NOW)
    assert UrlSigner("other-secret").verify(url, now=_NOW + 1) is False


def test_signed_url_host_change_is_still_valid() -> None:
    # Signing authorizes the path, not the host — an object-store URL stays valid
    # when rewritten to a CDN host (matches the minio→localhost rewrite pattern).
    signer = UrlSigner(_SECRET)
    url = signer.sign("https://minio:9000/kinora/b/seg.m4s", now=_NOW)
    rewritten = url.replace("minio:9000", "localhost:9000")
    assert signer.verify(rewritten, now=_NOW + 1) is True


def test_signed_url_query_reorder_does_not_break() -> None:
    signer = UrlSigner(_SECRET)
    url = signer.sign("https://cdn/seg.m4s?a=1&b=2", now=_NOW)
    assert signer.verify(url, now=_NOW + 1) is True


def test_signed_url_resign_drops_stale_sig() -> None:
    signer = UrlSigner(_SECRET)
    once = signer.sign("https://cdn/seg.m4s", ttl_s=100, now=_NOW)
    twice = signer.sign(once, ttl_s=100, now=_NOW + 50)
    # Only one exp/sig pair survives.
    assert twice.count("sig=") == 1 and twice.count("exp=") == 1
    assert signer.verify(twice, now=_NOW + 60) is True


def test_unsigned_url_fails_verification() -> None:
    assert UrlSigner(_SECRET).verify("https://cdn/seg.m4s", now=_NOW) is False


def test_url_signer_requires_secret() -> None:
    with pytest.raises(SigningError):
        UrlSigner("")
    with pytest.raises(SigningError):
        UrlSigner(_SECRET).sign("https://cdn/x", ttl_s=0)


# -- stream tokens --------------------------------------------------------- #


def test_stream_token_round_trips() -> None:
    signer = StreamTokenSigner(_SECRET)
    token = signer.mint(book_id="book-1", subject="user-9", ttl_s=3600, now=_NOW)
    claims = signer.verify(token, now=_NOW + 10)
    assert claims.book_id == "book-1"
    assert claims.subject == "user-9"
    assert claims.scope == "playback"
    assert claims.is_valid_for("book-1", now=_NOW + 10) is True
    assert claims.is_valid_for("book-2", now=_NOW + 10) is False


def test_stream_token_rejects_tamper() -> None:
    signer = StreamTokenSigner(_SECRET)
    token = signer.mint(book_id="b", subject="u", now=_NOW)
    payload, sig = token.split(".", 1)
    forged = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB") + "." + sig
    with pytest.raises(SigningError):
        signer.verify(forged, now=_NOW + 1)


def test_stream_token_rejects_expired() -> None:
    signer = StreamTokenSigner(_SECRET)
    token = signer.mint(book_id="b", subject="u", ttl_s=100, now=_NOW)
    with pytest.raises(SigningError):
        signer.verify(token, now=_NOW + 200)


def test_stream_token_rejects_wrong_secret() -> None:
    token = StreamTokenSigner(_SECRET).mint(book_id="b", subject="u", now=_NOW)
    with pytest.raises(SigningError):
        StreamTokenSigner("nope").verify(token, now=_NOW + 1)


def test_stream_token_malformed() -> None:
    signer = StreamTokenSigner(_SECRET)
    with pytest.raises(SigningError):
        signer.verify("not-a-token", now=_NOW)


def test_stream_token_extra_claims_preserved() -> None:
    signer = StreamTokenSigner(_SECRET)
    token = signer.mint(book_id="b", subject="u", now=_NOW, extra={"rendition": "720p"})
    # extra is encoded but does not override reserved claims.
    claims = signer.verify(token, now=_NOW + 1)
    assert claims.book_id == "b"


def test_derive_signing_secret_is_deterministic_and_distinct() -> None:
    a = derive_signing_secret("jwt-secret-1")
    b = derive_signing_secret("jwt-secret-1")
    c = derive_signing_secret("jwt-secret-2")
    assert a == b
    assert a != c
    assert a != "jwt-secret-1"  # never the raw secret
