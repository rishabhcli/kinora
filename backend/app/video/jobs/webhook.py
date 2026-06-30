"""Webhook authenticity verification + the storage-key convention for clips.

Two small, dependency-free pieces the engine needs at its edges:

* :class:`HmacWebhookVerifier` — a constant-time HMAC-SHA256 signature check, the
  shape every hosted video provider's webhook uses (a shared secret signs the raw
  body; the signature rides a header). The engine treats a failed verification as
  "drop the webhook, touch no job", so a spoofed callback can never terminalize a
  render. :class:`AllowAllVerifier` is the explicit opt-out for providers that do
  not sign (poll-only) — never the default.

* :func:`clip_storage_key` — where a persisted clip lands. It mirrors the
  existing ``clips/<book>/<shot>.mp4`` layout when the request metadata carries a
  ``book_id`` + ``shot_id``, and otherwise falls back to a job-id-scoped key so a
  metadata-less job still persists deterministically.
"""

from __future__ import annotations

import hashlib
import hmac

from .models import VideoJob


class HmacWebhookVerifier:
    """Constant-time HMAC-SHA256 verification of a signed webhook body.

    The signature is read from ``signature_header`` (default ``X-Kinora-Signature``)
    and compared, in constant time, against ``HMAC(secret, raw_body)``. An optional
    ``prefix`` (e.g. ``"sha256="``) is stripped before comparison. Hex and the
    common lower/upper casing are both accepted.
    """

    def __init__(
        self,
        secret: str,
        *,
        signature_header: str = "X-Kinora-Signature",
        prefix: str = "",
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._header = signature_header.lower()
        self._prefix = prefix

    def verify(self, *, raw_body: bytes, headers: dict[str, str]) -> bool:
        provided = self._extract(headers)
        if provided is None:
            return False
        expected = hmac.new(self._secret, raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided.lower(), expected.lower())

    def _extract(self, headers: dict[str, str]) -> str | None:
        for key, value in headers.items():
            if key.lower() == self._header:
                sig = value.strip()
                if self._prefix and sig.startswith(self._prefix):
                    sig = sig[len(self._prefix) :]
                return sig
        return None


class AllowAllVerifier:
    """A verifier that accepts everything — only for unsigned, poll-confirmed flows.

    Use this *exclusively* when the provider does not sign webhooks AND a webhook
    is treated as a hint that triggers an authoritative poll, never as a trusted
    completion on its own. It exists so the opt-out is explicit and greppable.
    """

    def verify(self, *, raw_body: bytes, headers: dict[str, str]) -> bool:  # noqa: D102
        return True


def clip_storage_key(job: VideoJob) -> str:
    """The durable object-storage key for ``job``'s clip.

    Prefers the canonical ``clips/<book_id>/<shot_id>.mp4`` layout (matching
    :class:`app.storage.object_store.Keys`) when both correlation ids are present;
    otherwise falls back to a job-scoped key so any job persists deterministically.
    """
    meta = job.request.metadata
    book_id = meta.get("book_id")
    shot_id = meta.get("shot_id")
    if book_id and shot_id:
        return f"clips/{book_id}/{shot_id}.mp4"
    return f"video-jobs/{job.provider}/{job.id}.mp4"


__all__ = ["AllowAllVerifier", "HmacWebhookVerifier", "clip_storage_key"]
