"""Invitation token machinery — the email-token accept flow (kinora.md §5).

An invitation hands a prospective collaborator a single **opaque, signed token**
they present to the accept endpoint. The token is self-describing and tamper-proof
without a DB round-trip for the *signature* check (an HMAC over the payload), but
the authoritative state still lives in the ``workspace_invitations`` row — the
token binds an invitation id + workspace + email + role + expiry so a forged or
mutated token fails verification before any lookup.

Format (URL-safe, no padding)::

    <b64url(payload_json)>.<b64url(hmac_sha256(payload_json))>

The payload carries the invitation id, workspace id, lower-cased email, role, and
an absolute expiry (epoch seconds). Verification recomputes the HMAC in constant
time, then checks expiry — both client-side cheap and forgery-resistant. The
secret is the app's ``jwt_secret`` (already required + 32-byte-enforced), reused
here rather than introducing a new config knob.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.workspaces.roles import Role

#: Default lifetime of an invitation token (7 days).
DEFAULT_INVITE_TTL_S = 7 * 24 * 3600


class InvitationTokenError(Exception):
    """Raised when an invitation token is malformed, forged, or expired."""


@dataclass(frozen=True, slots=True)
class InvitationClaims:
    """The verified payload carried by an invitation token."""

    invitation_id: str
    workspace_id: str
    email: str
    role: Role
    exp: int  # absolute expiry, epoch seconds

    def expires_at(self) -> datetime:
        """The expiry as a timezone-aware datetime."""
        return datetime.fromtimestamp(self.exp, tz=UTC)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()


def new_invitation_id() -> str:
    """A fresh opaque invitation id (independent of the token, for the DB row)."""
    return secrets.token_hex(16)


def create_invitation_token(
    *,
    invitation_id: str,
    workspace_id: str,
    email: str,
    role: Role,
    secret: str,
    ttl_s: int = DEFAULT_INVITE_TTL_S,
    now: datetime | None = None,
) -> tuple[str, datetime]:
    """Mint a signed invitation token; return ``(token, expires_at)``.

    The returned ``expires_at`` is what the caller stores on the
    ``workspace_invitations`` row so server-side state and the token agree.
    """
    moment = now or datetime.now(UTC)
    expires_at = moment + timedelta(seconds=ttl_s)
    payload = {
        "iid": invitation_id,
        "wid": workspace_id,
        "email": email.strip().lower(),
        "role": role.value,
        "exp": int(expires_at.timestamp()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = _sign(payload_bytes, secret)
    token = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(sig)}"
    return token, expires_at


def verify_invitation_token(
    token: str, secret: str, *, now: datetime | None = None
) -> InvitationClaims:
    """Verify a token's signature + expiry and return its claims.

    Raises :class:`InvitationTokenError` on any malformation, signature mismatch,
    or expiry — the accept endpoint maps that to a typed 4xx.
    """
    try:
        payload_b64, sig_b64 = token.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        provided_sig = _b64url_decode(sig_b64)
    except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
        raise InvitationTokenError("malformed invitation token") from exc

    expected_sig = _sign(payload_bytes, secret)
    if not hmac.compare_digest(provided_sig, expected_sig):
        raise InvitationTokenError("invitation token signature mismatch")

    try:
        payload = json.loads(payload_bytes)
        claims = InvitationClaims(
            invitation_id=str(payload["iid"]),
            workspace_id=str(payload["wid"]),
            email=str(payload["email"]),
            role=Role(str(payload["role"])),
            exp=int(payload["exp"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvitationTokenError("invalid invitation token payload") from exc

    moment = now or datetime.now(UTC)
    if claims.exp < int(moment.timestamp()):
        raise InvitationTokenError("invitation token expired")
    return claims


__all__ = [
    "DEFAULT_INVITE_TTL_S",
    "InvitationClaims",
    "InvitationTokenError",
    "create_invitation_token",
    "new_invitation_id",
    "verify_invitation_token",
]
