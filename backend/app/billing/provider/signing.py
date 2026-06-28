"""Signed-webhook HMAC helpers (Stripe-shaped ``t=...,v1=...`` scheme).

Inbound provider webhooks must be authenticated: anyone can POST to the webhook
URL, so we only trust a body whose HMAC-SHA256 signature (keyed by the shared
webhook secret) matches and whose signed timestamp is recent (replay window).

This mirrors Stripe's ``Stripe-Signature`` header format
(``t=<unix>,v1=<hex hmac>``) so the same verification works whether the events
come from the fake transport (which signs them with the same secret) or, in a
hypothetical real deployment, from Stripe. The signed message is
``f"{timestamp}.{raw_body}"`` exactly as Stripe specifies.

Comparison uses :func:`hmac.compare_digest` (constant-time) to avoid timing
leaks. No network, pure stdlib crypto.
"""

from __future__ import annotations

import hashlib
import hmac
import time

from app.billing.errors import WebhookVerificationError


def _signed_message(timestamp: int, payload: bytes) -> bytes:
    return f"{timestamp}.".encode() + payload


def sign_payload(payload: bytes, secret: str, *, timestamp: int | None = None) -> tuple[int, str]:
    """Return ``(timestamp, hex_signature)`` for ``payload`` under ``secret``."""
    ts = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode("utf-8"), _signed_message(ts, payload), hashlib.sha256)
    return ts, mac.hexdigest()


def build_signature_header(payload: bytes, secret: str, *, timestamp: int | None = None) -> str:
    """Build a ``t=<unix>,v1=<hex>`` signature header (the fake transport uses this)."""
    ts, sig = sign_payload(payload, secret, timestamp=timestamp)
    return f"t={ts},v1={sig}"


def _parse_header(header: str) -> tuple[int, list[str]]:
    """Parse ``t=...,v1=...[,v1=...]`` into (timestamp, [signatures])."""
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip()
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise WebhookVerificationError("invalid timestamp in signature header") from exc
        elif key == "v1":
            signatures.append(value)
    if timestamp is None:
        raise WebhookVerificationError("signature header missing timestamp")
    if not signatures:
        raise WebhookVerificationError("signature header missing v1 signature")
    return timestamp, signatures


def verify_signature(
    payload: bytes,
    signature_header: str,
    secret: str,
    *,
    tolerance_s: int = 300,
    now: int | None = None,
) -> int:
    """Verify a signed webhook; return the signed timestamp on success.

    Raises :class:`WebhookVerificationError` if the header is malformed, no
    signature matches, or the timestamp is outside the ``tolerance_s`` replay
    window. ``now`` is injectable for deterministic tests.
    """
    timestamp, signatures = _parse_header(signature_header)
    current = int(time.time()) if now is None else now
    if tolerance_s >= 0 and abs(current - timestamp) > tolerance_s:
        raise WebhookVerificationError("webhook timestamp outside the tolerance window")

    expected = hmac.new(
        secret.encode("utf-8"), _signed_message(timestamp, payload), hashlib.sha256
    ).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise WebhookVerificationError("no matching webhook signature")
    return timestamp


__all__ = [
    "build_signature_header",
    "sign_payload",
    "verify_signature",
]
