"""Signed / expiring CDN URL contract.

One place that knows how a stored key becomes a browser-reachable URL:

1. **Public base preference.** When a public read edge is configured
   (``S3_PUBLIC_BASE_URL`` — local MinIO, an OSS CDN, or any public bucket),
   serve a stable, un-signed URL. This is the default in local dev.
2. **Signed fallback.** Otherwise mint a time-limited presigned GET against the
   private API endpoint, with a clamped TTL.
3. **``minio:9000`` → ``localhost:9000`` rewrite.** Inside docker-compose the API
   talks to ``minio:9000``, but the *browser* must reach ``localhost:9000``. The
   project rewires this in the renderer; doing it here too means any URL minted
   server-side is already browser-correct (idempotent — a host without
   ``minio`` is left untouched).

The :class:`UrlSigner` Protocol is the seam the store/service depend on, so the
real :class:`app.storage.object_store.ObjectStore` (which already exposes
``presigned_get_url`` / ``public_url``) satisfies it without modification, and
tests can inject a fake.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

#: TTL clamps for signed URLs — never sign for less than a minute (clock skew)
#: nor longer than a week (S3 SigV4's own hard ceiling is 7 days).
MIN_TTL_S = 60
MAX_TTL_S = 7 * 24 * 3600
#: Sensible default link lifetime for a reading-room media URL.
DEFAULT_TTL_S = 3600


def clamp_ttl(ttl: int, *, lo: int = MIN_TTL_S, hi: int = MAX_TTL_S) -> int:
    """Clamp a requested TTL into the signable band."""
    return max(lo, min(hi, int(ttl)))


def rewrite_for_browser(url: str) -> str:
    """Rewrite an internal docker host to the browser-reachable one.

    Rewrites the ``minio:9000`` authority (compose-internal) to
    ``localhost:9000``. Idempotent and conservative: only the exact internal
    authority is touched, so a real ``localhost`` / CDN URL is returned verbatim.
    """
    return url.replace("://minio:9000", "://localhost:9000")


@runtime_checkable
class UrlSigner(Protocol):
    """The minimal URL surface the media layer needs (ObjectStore satisfies it)."""

    def presigned_get_url(self, key: str, ttl: int = 3600) -> str: ...

    def public_url(self, key: str) -> str | None: ...


def media_url(signer: UrlSigner, key: str, *, ttl: int = DEFAULT_TTL_S) -> str:
    """Resolve ``key`` to a browser-reachable URL (public base else signed).

    Prefers a stable public URL; falls back to a TTL-clamped signed URL. Either
    way the result is run through :func:`rewrite_for_browser` so a compose-host
    URL is corrected before it reaches the client.
    """
    public = signer.public_url(key)
    if public is not None:
        return rewrite_for_browser(public)
    signed = signer.presigned_get_url(key, ttl=clamp_ttl(ttl))
    return rewrite_for_browser(signed)


__all__ = [
    "DEFAULT_TTL_S",
    "MAX_TTL_S",
    "MIN_TTL_S",
    "UrlSigner",
    "clamp_ttl",
    "media_url",
    "rewrite_for_browser",
]
