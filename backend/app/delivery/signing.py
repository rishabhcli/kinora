"""Playback authorization — signed URLs + short-lived stream tokens.

Streaming manifests reference many child resources (the master playlist, each
media playlist / mpd, every segment + init). Two complementary mechanisms guard
them, both implemented here with **only the standard library** (HMAC-SHA256 +
constant-time compare) so the subsystem stays dependency-free and unit-testable
with no network:

* **Signed URLs** (:class:`UrlSigner`) — append an ``exp`` + ``sig`` query pair
  to a resource URL; an edge/CDN (or our own handler) recomputes the HMAC over
  the path + expiry and rejects a tampered or expired URL. This is the
  per-resource guard that lets a manifest be served from object storage / a CDN
  without a per-request auth round-trip.
* **Stream tokens** (:class:`StreamTokenSigner`) — a compact signed token
  (``base64url(payload).base64url(sig)``) scoping playback to one ``book_id``
  for one viewer for a short window. The player presents it once; the manifest
  handler validates it and *mints signed URLs* for the children. This is the
  hook the playback route wires in — it never embeds a long-lived credential in
  a URL that ends up in CDN logs.

The signing secret defaults to deriving from the app's ``jwt_secret`` (so a
deployment that already rotated its JWT secret rotates playback signing too),
but a caller can pass any secret — keeping this module decoupled from the auth
package. Tokens are intentionally *not* JWTs: they're a minimal internal format,
so there is no dependency on PyJWT and no confusion with auth's access tokens.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.delivery.errors import SigningError

#: Default playback-URL lifetime — long enough to stream a buffer-ahead window,
#: short enough that a leaked URL expires quickly.
DEFAULT_URL_TTL_S = 3600
#: Default stream-token lifetime (a viewing session re-mints as it streams).
DEFAULT_TOKEN_TTL_S = 21600  # 6h


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def derive_signing_secret(jwt_secret: str) -> str:
    """Derive a dedicated playback-signing secret from the app JWT secret.

    A distinct domain-separated derivation so the playback signer never reuses
    the raw JWT secret directly (defence in depth — a playback-signing oracle
    must not leak the JWT secret).
    """
    digest = hashlib.sha256(b"kinora-delivery-signing:" + jwt_secret.encode("utf-8")).hexdigest()
    return digest


class UrlSigner:
    """HMAC-signs and verifies resource URLs with an expiry.

    The signature is computed over the **path + sorted query (excluding sig)** +
    ``exp``, so reordering query params or changing the path invalidates it.
    Hosts are excluded so a URL signed for object storage stays valid when
    rewritten to a CDN host (the path is what's authorized).
    """

    EXP_PARAM = "exp"
    SIG_PARAM = "sig"

    def __init__(self, secret: str) -> None:
        if not secret:
            raise SigningError("UrlSigner requires a non-empty secret")
        self._secret = secret.encode("utf-8")

    def _signing_input(self, path: str, query_pairs: list[tuple[str, str]], exp: int) -> bytes:
        # Exclude any pre-existing sig; include exp deterministically.
        pairs = [(k, v) for k, v in query_pairs if k != self.SIG_PARAM]
        if not any(k == self.EXP_PARAM for k, _ in pairs):
            pairs.append((self.EXP_PARAM, str(exp)))
        canonical = urlencode(sorted(pairs))
        return f"{path}?{canonical}".encode()

    def _sign(self, message: bytes) -> str:
        return _b64url_encode(hmac.new(self._secret, message, hashlib.sha256).digest())

    def sign(self, url: str, *, ttl_s: int = DEFAULT_URL_TTL_S, now: float | None = None) -> str:
        """Return ``url`` with ``exp`` + ``sig`` query params appended.

        Idempotent on the path: signing an already-signed URL re-signs with a
        fresh expiry (any stale ``exp``/``sig`` are dropped before re-signing).
        """
        if ttl_s <= 0:
            raise SigningError("ttl_s must be positive")
        now = time.time() if now is None else now
        exp = int(now) + ttl_s
        parts = urlsplit(url)
        pairs = [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in (self.EXP_PARAM, self.SIG_PARAM)
        ]
        pairs.append((self.EXP_PARAM, str(exp)))
        sig = self._sign(self._signing_input(parts.path, pairs, exp))
        pairs.append((self.SIG_PARAM, sig))
        return urlunsplit(
            (parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment)
        )

    def verify(self, url: str, *, now: float | None = None) -> bool:
        """True iff the URL carries a valid, unexpired signature for its path."""
        now = time.time() if now is None else now
        parts = urlsplit(url)
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        params = dict(pairs)
        exp_raw = params.get(self.EXP_PARAM)
        sig = params.get(self.SIG_PARAM)
        if exp_raw is None or sig is None:
            return False
        try:
            exp = int(exp_raw)
        except ValueError:
            return False
        if exp < now:
            return False
        expected = self._sign(self._signing_input(parts.path, pairs, exp))
        return hmac.compare_digest(expected, sig)


class StreamToken:
    """A decoded playback token's claims."""

    __slots__ = ("book_id", "subject", "exp", "scope")

    def __init__(self, *, book_id: str, subject: str, exp: int, scope: str) -> None:
        self.book_id = book_id
        self.subject = subject
        self.exp = exp
        self.scope = scope

    def is_valid_for(self, book_id: str, *, now: float) -> bool:
        return self.book_id == book_id and self.exp >= now and self.scope == "playback"

    def to_claims(self) -> dict[str, object]:
        return {"book_id": self.book_id, "sub": self.subject, "exp": self.exp, "scope": self.scope}


class StreamTokenSigner:
    """Mints + verifies compact ``payload.sig`` playback tokens (HMAC-SHA256)."""

    def __init__(self, secret: str) -> None:
        if not secret:
            raise SigningError("StreamTokenSigner requires a non-empty secret")
        self._secret = secret.encode("utf-8")

    def _sign(self, payload_b64: str) -> str:
        return _b64url_encode(
            hmac.new(self._secret, payload_b64.encode("ascii"), hashlib.sha256).digest()
        )

    def mint(
        self,
        *,
        book_id: str,
        subject: str,
        ttl_s: int = DEFAULT_TOKEN_TTL_S,
        scope: str = "playback",
        now: float | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> str:
        """Mint a token scoping ``subject`` to play ``book_id`` for ``ttl_s`` seconds."""
        if ttl_s <= 0:
            raise SigningError("ttl_s must be positive")
        now = time.time() if now is None else now
        claims: dict[str, object] = {
            "book_id": book_id,
            "sub": subject,
            "exp": int(now) + ttl_s,
            "scope": scope,
        }
        if extra:
            claims.update({k: v for k, v in extra.items() if k not in claims})
        payload = _b64url_encode(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
        return f"{payload}.{self._sign(payload)}"

    def verify(self, token: str, *, now: float | None = None) -> StreamToken:
        """Decode + verify a token, returning its claims.

        Raises:
            SigningError: if the token is malformed, tampered, or expired.
        """
        now = time.time() if now is None else now
        try:
            payload_b64, sig = token.split(".", 1)
        except ValueError as exc:
            raise SigningError("malformed stream token") from exc
        expected = self._sign(payload_b64)
        if not hmac.compare_digest(expected, sig):
            raise SigningError("stream token signature mismatch")
        try:
            claims = json.loads(_b64url_decode(payload_b64))
        except (ValueError, json.JSONDecodeError) as exc:
            raise SigningError("undecodable stream token payload") from exc
        exp = int(claims.get("exp", 0))
        if exp < now:
            raise SigningError("stream token expired")
        return StreamToken(
            book_id=str(claims.get("book_id", "")),
            subject=str(claims.get("sub", "")),
            exp=exp,
            scope=str(claims.get("scope", "playback")),
        )
