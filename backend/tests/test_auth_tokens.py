"""Unit tests for :mod:`app.auth.tokens` — JWT issue/verify + refresh rotation.

No infrastructure: the :class:`TokenService` only needs settings and an in-memory
revocation store fake.
"""

from __future__ import annotations

import time

import jwt
import pytest

from app.api.security import decode_access_token as legacy_decode
from app.auth.errors import MfaInvalid, TokenInvalid
from app.auth.tokens import TokenService
from app.core.config import Settings


def _settings(**over: object) -> Settings:
    base: dict[str, object] = {
        "dashscope_api_key": "test",
        "app_env": "local",
        "jwt_secret": "unit-test-secret-comfortably-longer-than-32-bytes",
        "access_token_ttl_s": 3600,
    }
    base.update(over)
    return Settings(**base)  # type: ignore[arg-type]


class _MemRevocations:
    def __init__(self) -> None:
        self._set: set[str] = set()

    async def is_revoked(self, jti: str) -> bool:
        return jti in self._set

    async def revoke(self, jti: str, *, ttl_s: int) -> None:
        self._set.add(jti)


def test_access_token_roundtrip_with_rich_claims() -> None:
    svc = TokenService(_settings())
    token, claims = svc.issue_access_token(
        "user-1",
        session_id="sess-9",
        roles=["admin", "reader"],
        scopes=["books:read"],
        tenant="acme",
    )
    decoded = svc.decode_access_token(token)
    assert decoded.sub == "user-1"
    assert decoded.sid == "sess-9"
    assert decoded.roles == ("admin", "reader")
    assert decoded.scopes == ("books:read",)
    assert decoded.tenant == "acme"
    assert decoded.jti == claims.jti


def test_new_token_is_legacy_decodable() -> None:
    """A token from the new issuer must verify with the legacy decoder (compat)."""
    settings = _settings()
    svc = TokenService(settings)
    token, _ = svc.issue_access_token("user-7", roles=["reader"])
    legacy = legacy_decode(token, settings)
    assert legacy.sub == "user-7"


def test_legacy_token_is_new_decodable() -> None:
    """A legacy token (no aud/iss) must verify with the new decoder (compat)."""
    from app.api.security import create_access_token

    settings = _settings()
    legacy_token = create_access_token("user-legacy", settings)
    svc = TokenService(settings)
    claims = svc.decode_access_token(legacy_token)
    assert claims.sub == "user-legacy"
    assert claims.roles == ()


def test_decode_rejects_tampered_token() -> None:
    svc = TokenService(_settings())
    token, _ = svc.issue_access_token("user-1")
    with pytest.raises(TokenInvalid):
        svc.decode_access_token(token + "x")


def test_decode_rejects_wrong_secret() -> None:
    svc_a = TokenService(_settings(jwt_secret="secret-A-aaaaaaaaaaaaaaaaaaaaaaaaaa"))
    token, _ = svc_a.issue_access_token("u")
    other = TokenService(_settings(jwt_secret="secret-B-bbbbbbbbbbbbbbbbbbbbbbbbbb"))
    with pytest.raises(TokenInvalid):
        other.decode_access_token(token)


def test_expired_token_rejected() -> None:
    svc = TokenService(_settings())
    token, _ = svc.issue_access_token("user-1", expires_in_s=-10)
    with pytest.raises(TokenInvalid):
        svc.decode_access_token(token)


def test_audience_enforced_when_present() -> None:
    """A token minted for a different audience is rejected."""
    settings = _settings(jwt_audience="kinora-api")
    svc = TokenService(settings)
    token, _ = svc.issue_access_token("u")
    # Forge a token with a wrong audience but the right signature.
    forged = jwt.encode(
        {"sub": "u", "aud": "evil", "iat": int(time.time()), "exp": int(time.time()) + 60},
        settings.jwt_secret,
        algorithm=settings.jwt_alg,
    )
    assert svc.decode_access_token(token).sub == "u"
    with pytest.raises(TokenInvalid):
        svc.decode_access_token(forged)


async def test_verify_rejects_revoked_jti() -> None:
    revocations = _MemRevocations()
    svc = TokenService(_settings(), revocations=revocations)
    token, claims = svc.issue_access_token("user-1")
    assert (await svc.verify_access_token(token)).sub == "user-1"
    await svc.revoke_access_jti(claims)
    with pytest.raises(TokenInvalid):
        await svc.verify_access_token(token)


def test_refresh_token_minting_and_rotation_family() -> None:
    svc = TokenService(_settings())
    first = svc.issue_refresh_token()
    assert first.digest == TokenService.refresh_digest(first.token)
    # Rotation keeps the family id.
    rotated = svc.issue_refresh_token(family_id=first.family_id)
    assert rotated.family_id == first.family_id
    assert rotated.token != first.token
    assert rotated.digest != first.digest
    # A fresh family differs.
    fresh = svc.issue_refresh_token()
    assert fresh.family_id != first.family_id


def test_mfa_challenge_roundtrip() -> None:
    svc = TokenService(_settings())
    tok = svc.issue_mfa_challenge("user-9")
    assert svc.decode_mfa_challenge(tok) == "user-9"


def test_mfa_challenge_rejects_access_token() -> None:
    svc = TokenService(_settings())
    access, _ = svc.issue_access_token("u")
    with pytest.raises(MfaInvalid):
        svc.decode_mfa_challenge(access)
