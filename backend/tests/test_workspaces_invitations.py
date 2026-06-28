"""Pure-unit tests for the invitation token machinery (no infra).

Covers minting, round-trip verification, tamper-resistance, expiry, and the
malformed-token paths — the security-critical half of the email-token flow.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.workspaces.invitations import (
    InvitationTokenError,
    create_invitation_token,
    new_invitation_id,
    verify_invitation_token,
)
from app.workspaces.roles import Role

SECRET = "kinora-test-secret-which-is-comfortably-32-bytes-long"


def _mint(**overrides: object) -> tuple[str, datetime]:
    kwargs: dict = {
        "invitation_id": new_invitation_id(),
        "workspace_id": "ws_123",
        "email": "Invitee@Example.com",
        "role": Role.EDITOR,
        "secret": SECRET,
    }
    kwargs.update(overrides)
    return create_invitation_token(**kwargs)


def test_round_trip() -> None:
    iid = new_invitation_id()
    token, expires_at = _mint(invitation_id=iid)
    claims = verify_invitation_token(token, SECRET)
    assert claims.invitation_id == iid
    assert claims.workspace_id == "ws_123"
    assert claims.email == "invitee@example.com"  # normalised lower-case
    assert claims.role == Role.EDITOR
    # The token's exp is whole epoch seconds, so it matches at second precision.
    assert int(claims.expires_at().timestamp()) == int(expires_at.timestamp())


def test_new_invitation_id_is_unique_and_hex() -> None:
    ids = {new_invitation_id() for _ in range(100)}
    assert len(ids) == 100
    assert all(len(i) == 32 and all(c in "0123456789abcdef" for c in i) for i in ids)


def test_signature_mismatch_with_wrong_secret() -> None:
    token, _ = _mint()
    with pytest.raises(InvitationTokenError, match="signature"):
        verify_invitation_token(token, "a-different-but-also-32-byte-long-secret-x")


def test_tampered_payload_rejected() -> None:
    token, _ = _mint()
    head, _, sig = token.partition(".")
    # Flip a character in the payload; signature no longer matches.
    mutated = (head[:-1] + ("A" if head[-1] != "A" else "B")) + "." + sig
    with pytest.raises(InvitationTokenError):
        verify_invitation_token(mutated, SECRET)


def test_malformed_token_rejected() -> None:
    for bad in ("", "no-dot", "only.", ".only", "!!!.???"):
        with pytest.raises(InvitationTokenError):
            verify_invitation_token(bad, SECRET)


def test_expired_token_rejected() -> None:
    past = datetime.now(UTC) - timedelta(days=30)
    token, _ = _mint(ttl_s=3600, now=past)
    with pytest.raises(InvitationTokenError, match="expired"):
        verify_invitation_token(token, SECRET)


def test_verify_at_explicit_time() -> None:
    base = datetime(2026, 1, 1, tzinfo=UTC)
    token, expires_at = _mint(ttl_s=3600, now=base)
    # Valid just before expiry, invalid just after.
    just_before = expires_at - timedelta(seconds=1)
    just_after = expires_at + timedelta(seconds=1)
    assert verify_invitation_token(token, SECRET, now=just_before).workspace_id == "ws_123"
    with pytest.raises(InvitationTokenError, match="expired"):
        verify_invitation_token(token, SECRET, now=just_after)
