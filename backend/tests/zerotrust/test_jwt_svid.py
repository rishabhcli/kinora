"""JWT-SVID minting + strict verification tests."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.zerotrust.identity import (
    JwtSvidMinter,
    JwtSvidVerifier,
    ManualClock,
    SigningKey,
    SpiffeId,
    TokenAudienceError,
    TokenError,
    TokenExpiredError,
    TokenSignatureError,
)
from tests.zerotrust.conftest import TRUST_DOMAIN


def _minter(clock: ManualClock, key: SigningKey) -> JwtSvidMinter:
    return JwtSvidMinter(
        signing_key=key, key_id="kid-1", issuer=f"spiffe://{TRUST_DOMAIN}", clock=clock
    )


def test_mint_and_verify_round_trip(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/render-worker")
    svid = minter.mint(sid, f"spiffe://{TRUST_DOMAIN}/mcp", ttl=timedelta(minutes=5))
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    decoded = verifier.verify(svid.token, audience=f"spiffe://{TRUST_DOMAIN}/mcp")
    assert decoded.spiffe_id == sid
    assert f"spiffe://{TRUST_DOMAIN}/mcp" in decoded.audience


def test_multiple_audiences(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x")
    svid = minter.mint(sid, ["aud-a", "aud-b"])
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    assert verifier.verify(svid.token, audience="aud-a").spiffe_id == sid
    assert verifier.verify(svid.token, audience="aud-b").spiffe_id == sid


def test_wrong_audience_rejected(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    svid = minter.mint(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), "aud-a")
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    with pytest.raises(TokenAudienceError):
        verifier.verify(svid.token, audience="aud-wrong")


def test_expired_token_rejected(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    svid = minter.mint(
        SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), "aud", ttl=timedelta(minutes=1)
    )
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    verifier.verify(svid.token, audience="aud")
    clock.advance(minutes=5)
    with pytest.raises(TokenExpiredError):
        verifier.verify(svid.token, audience="aud")


def test_unknown_kid_rejected(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    svid = minter.mint(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), "aud")
    from app.zerotrust.identity import JwtKeyRegistry

    empty = JwtSvidVerifier(JwtKeyRegistry(), clock=clock)
    with pytest.raises(TokenSignatureError):
        empty.verify(svid.token, audience="aud")


def test_tampered_payload_rejected(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    svid = minter.mint(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), "aud")
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    header, payload, sig = svid.token.split(".")
    # flip a char in the payload
    tampered = f"{header}.{payload[:-2]}AA.{sig}"
    with pytest.raises(TokenError):
        verifier.verify(tampered, audience="aud")


def test_alg_confusion_blocked(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    """A token whose header alg doesn't match the resolved key is rejected."""

    import base64
    import json

    minter = _minter(clock, ec_jwt_key)
    svid = minter.mint(SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"), "aud")
    header, payload, sig = svid.token.split(".")
    # rewrite the header alg to 'none'
    forged_header = base64.urlsafe_b64encode(
        json.dumps({"typ": "JWT", "alg": "none", "kid": "kid-1"}).encode()
    ).rstrip(b"=").decode()
    forged = f"{forged_header}.{payload}.{sig}"
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    with pytest.raises(TokenSignatureError):
        verifier.verify(forged, audience="aud")


def test_ed25519_jwt_round_trip(clock: ManualClock, ed_ca_key: SigningKey) -> None:
    minter = JwtSvidMinter(signing_key=ed_ca_key, key_id="ed-1", clock=clock)
    sid = SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x")
    svid = minter.mint(sid, "aud")
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    assert verifier.verify(svid.token, audience="aud").spiffe_id == sid


def test_extra_claims_cannot_override_reserved(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    with pytest.raises(TokenError):
        minter.mint(
            SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"),
            "aud",
            extra_claims={"sub": "spoofed"},
        )


def test_extra_claims_pass_through(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    minter = _minter(clock, ec_jwt_key)
    svid = minter.mint(
        SpiffeId.parse(f"spiffe://{TRUST_DOMAIN}/x"),
        "aud",
        extra_claims={"scope": "render"},
    )
    verifier = JwtSvidVerifier(minter.registry(), clock=clock)
    decoded = verifier.verify(svid.token, audience="aud")
    assert decoded.claims["scope"] == "render"


def test_malformed_token_segments(clock: ManualClock, ec_jwt_key: SigningKey) -> None:
    verifier = JwtSvidVerifier(_minter(clock, ec_jwt_key).registry(), clock=clock)
    with pytest.raises(TokenError):
        verifier.verify("only.two")
