"""Typed failures raised inside the webhook ingress gateway.

These are *internal* to the subsystem; the route translates them into the
gateway's ``{"error": {...}}`` envelope with the right HTTP status (401 bad
signature, 4xx malformed, 413 too large, 429 rate-limited). Keeping them as a
small hierarchy lets the route map by ``isinstance`` instead of string-matching.

Crucially, no error message ever embeds a secret (the signing key, the raw
signature header value) — only what is safe to surface to a misbehaving caller.
"""

from __future__ import annotations


class WebhookIngressError(Exception):
    """Base for every gateway-level rejection."""


class UnknownProviderError(WebhookIngressError):
    """The URL named a provider with no registered signing config (404/400)."""


class SignatureError(WebhookIngressError):
    """The signature was missing, malformed, or did not verify (401)."""


class ReplayError(SignatureError):
    """The signed timestamp was outside the replay-tolerance window (401).

    A subtype of :class:`SignatureError` because, like a bad signature, it means
    "do not trust this request" and maps to the same 401 — but it is named
    distinctly so logs/metrics can tell a *stale* delivery from a *forged* one.
    """


class PayloadTooLargeError(WebhookIngressError):
    """The raw body exceeded the configured ingress size guard (413)."""


class MalformedPayloadError(WebhookIngressError):
    """The body verified but could not be parsed into a canonical callback (422)."""


__all__ = [
    "MalformedPayloadError",
    "PayloadTooLargeError",
    "ReplayError",
    "SignatureError",
    "UnknownProviderError",
    "WebhookIngressError",
]
