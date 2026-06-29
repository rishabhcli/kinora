"""JWT-SVID minting + verification (the token form of a workload identity).

For peers that speak bearer tokens rather than mTLS (an API gateway, a mesh
sidecar, an OAuth-style resource server), a workload presents a **JWT-SVID**: a
compact JWS whose ``sub`` is the SPIFFE ID and whose ``aud`` names the intended
audience(s). This module mints and verifies them with the same EC-P256 / Ed25519
keys the CA uses, implementing just enough JOSE on the stdlib + :mod:`keys` so the
package pulls in no JWT dependency of its own.

Verification is strict by design:

* the ``alg`` header must match the key's algorithm (no ``none``, no alg
  confusion — we resolve the key by ``kid`` from a :class:`JwtKeyRegistry`, then
  require its native ``alg``);
* the signature must verify;
* ``exp`` / ``nbf`` are checked against the clock with a small leeway;
* the requested audience must appear in the token's ``aud`` set.

EC signatures are emitted in the JOSE **raw R||S** form (not the DER the
:mod:`cryptography` signer returns), and converted back to DER for verification,
so a JWT-SVID is interoperable with any RFC 7515 verifier.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

from app.zerotrust.identity.clock import Clock, SystemClock
from app.zerotrust.identity.errors import (
    TokenAudienceError,
    TokenError,
    TokenExpiredError,
    TokenSignatureError,
)
from app.zerotrust.identity.keys import KeyAlgorithm, PublicKey, SigningKey
from app.zerotrust.identity.spiffe import SpiffeId
from app.zerotrust.identity.svid import JwtSvid

#: Default JWT-SVID lifetime (short, like the X.509 leaves).
DEFAULT_JWT_TTL = timedelta(minutes=5)
#: Clock-skew leeway when validating exp/nbf.
DEFAULT_LEEWAY = timedelta(seconds=30)
#: EC P-256 raw signature component length (32-byte R, 32-byte S).
_EC_COMPONENT_BYTES = 32


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _der_to_jose(der: bytes) -> bytes:
    r, s = decode_dss_signature(der)
    return r.to_bytes(_EC_COMPONENT_BYTES, "big") + s.to_bytes(_EC_COMPONENT_BYTES, "big")


def _jose_to_der(raw: bytes) -> bytes:
    if len(raw) != 2 * _EC_COMPONENT_BYTES:
        raise TokenSignatureError("malformed ES256 signature length")
    r = int.from_bytes(raw[:_EC_COMPONENT_BYTES], "big")
    s = int.from_bytes(raw[_EC_COMPONENT_BYTES:], "big")
    return encode_dss_signature(r, s)


@dataclass(slots=True)
class JwtKeyRegistry:
    """A ``kid`` → public-key map (the JWT-SVID analogue of a trust bundle)."""

    _keys: dict[str, PublicKey] = field(default_factory=dict)

    def add(self, kid: str, key: PublicKey) -> JwtKeyRegistry:
        self._keys[kid] = key
        return self

    def get(self, kid: str) -> PublicKey | None:
        return self._keys.get(kid)

    def kids(self) -> frozenset[str]:
        return frozenset(self._keys)


@dataclass(slots=True)
class JwtSvidMinter:
    """Mints JWT-SVIDs signed by one keypair under a stable ``kid``."""

    signing_key: SigningKey
    key_id: str
    issuer: str | None = None
    clock: Clock = field(default_factory=SystemClock)

    def public_key(self) -> PublicKey:
        return self.signing_key.public_key()

    def registry(self) -> JwtKeyRegistry:
        """A single-key registry for verifiers (the JWT trust anchor)."""

        return JwtKeyRegistry().add(self.key_id, self.public_key())

    def mint(
        self,
        spiffe_id: SpiffeId,
        audience: str | Iterable[str],
        *,
        ttl: timedelta = DEFAULT_JWT_TTL,
        extra_claims: dict[str, object] | None = None,
    ) -> JwtSvid:
        """Mint a JWT-SVID for *spiffe_id* scoped to *audience*."""

        auds = [audience] if isinstance(audience, str) else list(audience)
        if not auds:
            raise TokenError("JWT-SVID requires at least one audience")
        now = self.clock.now()
        exp = now + ttl
        header = {
            "typ": "JWT",
            "alg": self.signing_key.algorithm.jose_alg,
            "kid": self.key_id,
        }
        claims: dict[str, object] = {
            "sub": spiffe_id.uri,
            "aud": auds if len(auds) > 1 else auds[0],
            "iat": _epoch(now),
            "nbf": _epoch(now),
            "exp": _epoch(exp),
        }
        if self.issuer is not None:
            claims["iss"] = self.issuer
        if extra_claims:
            for reserved in ("sub", "aud", "exp", "iat", "nbf"):
                if reserved in extra_claims:
                    raise TokenError(f"extra_claims may not override reserved {reserved!r}")
            claims.update(extra_claims)
        signing_input = (
            _b64u_encode(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64u_encode(json.dumps(claims, separators=(",", ":")).encode())
        )
        raw_sig = self.signing_key.sign(signing_input.encode("ascii"))
        if self.signing_key.algorithm is KeyAlgorithm.EC_P256:
            raw_sig = _der_to_jose(raw_sig)
        token = signing_input + "." + _b64u_encode(raw_sig)
        return JwtSvid(
            spiffe_id=spiffe_id,
            audience=frozenset(auds),
            token=token,
            issued_at=now,
            expires_at=exp,
            key_id=self.key_id,
            claims=claims,
        )


@dataclass(slots=True)
class JwtSvidVerifier:
    """Verifies JWT-SVIDs against a :class:`JwtKeyRegistry`."""

    registry: JwtKeyRegistry
    clock: Clock = field(default_factory=SystemClock)
    leeway: timedelta = DEFAULT_LEEWAY

    def verify(
        self,
        token: str,
        *,
        audience: str | None = None,
        at: datetime | None = None,
    ) -> JwtSvid:
        """Verify *token*; return the decoded :class:`JwtSvid` or raise."""

        parts = token.split(".")
        if len(parts) != 3:
            raise TokenError("JWT-SVID must have three segments")
        h_b64, p_b64, s_b64 = parts
        try:
            header = json.loads(_b64u_decode(h_b64))
            claims = json.loads(_b64u_decode(p_b64))
            signature = _b64u_decode(s_b64)
        except (ValueError, json.JSONDecodeError) as exc:
            raise TokenError("malformed JWT-SVID encoding") from exc
        if header.get("typ") not in ("JWT", None):
            raise TokenError("unexpected JWT-SVID typ")

        kid = header.get("kid")
        if not isinstance(kid, str):
            raise TokenError("JWT-SVID missing kid header")
        key = self.registry.get(kid)
        if key is None:
            raise TokenSignatureError(f"unknown kid {kid!r}")
        alg = header.get("alg")
        if alg != key.algorithm.jose_alg:
            # blocks alg confusion + 'none'
            raise TokenSignatureError(f"alg {alg!r} does not match key for kid {kid!r}")

        signing_input = (h_b64 + "." + p_b64).encode("ascii")
        verify_sig = (
            _jose_to_der(signature)
            if key.algorithm is KeyAlgorithm.EC_P256
            else signature
        )
        if not key.verify(verify_sig, signing_input):
            raise TokenSignatureError("JWT-SVID signature did not verify")

        now = at or self.clock.now()
        now_epoch = _epoch(now)
        leeway = int(self.leeway.total_seconds())
        exp = claims.get("exp")
        if not isinstance(exp, int):
            raise TokenError("JWT-SVID missing exp")
        if now_epoch > exp + leeway:
            raise TokenExpiredError("JWT-SVID is expired")
        nbf = claims.get("nbf")
        if isinstance(nbf, int) and now_epoch + leeway < nbf:
            raise TokenError("JWT-SVID not yet valid")

        sub = claims.get("sub")
        if not isinstance(sub, str):
            raise TokenError("JWT-SVID missing sub")
        spiffe_id = SpiffeId.parse(sub)

        aud_claim = claims.get("aud")
        if isinstance(aud_claim, str):
            auds = frozenset({aud_claim})
        elif isinstance(aud_claim, list):
            auds = frozenset(str(a) for a in aud_claim)
        else:
            raise TokenError("JWT-SVID missing aud")
        if audience is not None and audience not in auds:
            raise TokenAudienceError(
                f"JWT-SVID audience {sorted(auds)} does not include {audience!r}"
            )

        iat = claims.get("iat")
        issued_at = (
            datetime.fromtimestamp(iat, tz=now.tzinfo) if isinstance(iat, int) else now
        )
        return JwtSvid(
            spiffe_id=spiffe_id,
            audience=auds,
            token=token,
            issued_at=issued_at,
            expires_at=datetime.fromtimestamp(exp, tz=now.tzinfo),
            key_id=kid,
            claims=claims,
        )


__all__ = [
    "DEFAULT_JWT_TTL",
    "DEFAULT_LEEWAY",
    "JwtKeyRegistry",
    "JwtSvidMinter",
    "JwtSvidVerifier",
]
