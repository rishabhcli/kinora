"""Provider-abstracted signed-URL generation (expiring, range-request friendly).

A reader gets a single browser-reachable URL for a clip; *how* it is signed
varies by provider (S3/OSS SigV4 presign, a CDN edge-token, or a stable public
base). This module hides that behind one call so the resolver doesn't care.

Design:

* Reuse the :mod:`app.media.urls` contract — :func:`clamp_ttl` (60 s..7 d band)
  and :func:`rewrite_for_browser` (``minio:9000`` → ``localhost:9000``) — so a
  CDN-signed URL is browser-correct the same way a media URL is.
* **Range-friendly by construction.** Signed GETs here authorise the *object*,
  not a byte range, so the client may issue ``Range:`` requests against the same
  URL for progressive / seek playback (exactly what the reading-room player
  needs). :data:`RANGE_FRIENDLY_METHODS` documents the contract; a signer must
  not bind ``Range`` into the signature.
* A signed URL carries an explicit ``expires_at`` (epoch seconds) so callers can
  decide to re-sign before handing a stale link to a slow reader.

Two seams: :class:`UrlSigner` (a region store that can presign — the existing
:class:`ObjectStore` satisfies it) and :class:`EdgeTokenSigner` (a CDN that
mints edge tokens). The :class:`SignedUrl` value object is what the resolver
returns to the API layer.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from app.media.urls import DEFAULT_TTL_S, clamp_ttl, rewrite_for_browser

#: HTTP methods a signed media URL must keep range-request friendly: a single
#: signed GET authorises the whole object so the client may seek with ``Range``.
RANGE_FRIENDLY_METHODS = ("GET", "HEAD")


class SignedUrl(BaseModel):
    """An expiring, browser-reachable URL for one object."""

    model_config = ConfigDict(frozen=True)

    url: str
    key: str
    region_id: str
    #: Epoch seconds at which the signature expires. ``None`` for a stable public
    #: URL (no expiry).
    expires_at: float | None = None
    #: Whether the client may issue ``Range:`` requests against this URL.
    range_supported: bool = True

    def is_expired(self, now: float, *, skew_s: float = 0.0) -> bool:
        """Whether the URL has expired as of ``now`` (with optional clock skew).

        A public URL (no ``expires_at``) never expires.
        """
        if self.expires_at is None:
            return False
        return now >= (self.expires_at - skew_s)


@runtime_checkable
class UrlSigner(Protocol):
    """The minimal presign surface a region store must expose (ObjectStore fits)."""

    @property
    def region_id(self) -> str: ...

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str: ...

    def public_url(self, key: str) -> str | None: ...


@runtime_checkable
class EdgeTokenSigner(Protocol):
    """A CDN edge able to mint a tokenised URL for a key (e.g. CloudFront cookies)."""

    @property
    def region_id(self) -> str: ...

    def edge_url(self, key: str, *, ttl: int) -> str:
        """A signed edge URL valid for ``ttl`` seconds."""
        ...


def sign_url(
    signer: UrlSigner,
    key: str,
    *,
    now: float,
    ttl: int = DEFAULT_TTL_S,
    edge: EdgeTokenSigner | None = None,
) -> SignedUrl:
    """Mint a browser-reachable :class:`SignedUrl` for ``key`` in a region.

    Resolution order:

    1. **Stable public base** (``public_url``) — un-signed, no expiry (the local
       MinIO / public-bucket default).
    2. **Edge token** — when an ``edge`` signer is supplied, prefer its CDN-edge
       URL (served closest to the reader) over a raw origin presign.
    3. **Origin presign** — a TTL-clamped S3/OSS SigV4 GET.

    The TTL is clamped to the signable band; ``expires_at`` is reported relative
    to ``now`` so the value object is self-describing for re-sign decisions. The
    URL is always run through :func:`rewrite_for_browser`.
    """
    clamped = clamp_ttl(ttl)
    public = signer.public_url(key)
    if public is not None:
        return SignedUrl(
            url=rewrite_for_browser(public),
            key=key,
            region_id=signer.region_id,
            expires_at=None,
            range_supported=True,
        )
    if edge is not None:
        return SignedUrl(
            url=rewrite_for_browser(edge.edge_url(key, ttl=clamped)),
            key=key,
            region_id=signer.region_id,
            expires_at=now + clamped,
            range_supported=True,
        )
    return SignedUrl(
        url=rewrite_for_browser(signer.presigned_get_url(key, ttl=clamped)),
        key=key,
        region_id=signer.region_id,
        expires_at=now + clamped,
        range_supported=True,
    )


__all__ = [
    "RANGE_FRIENDLY_METHODS",
    "EdgeTokenSigner",
    "SignedUrl",
    "UrlSigner",
    "sign_url",
]
