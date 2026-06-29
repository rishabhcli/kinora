"""Artifact integrity + signing — content hashes and detached signatures.

A published plugin artifact is the pair *(manifest, source)*. The registry must
be able to (a) name an artifact by its content so an upload is idempotent and
tamper-evident, and (b) verify that a trusted publisher signed exactly those
bytes. Both are done with stdlib primitives only — no external crypto package —
so the platform stays dependency-light and the tests are deterministic.

* **Content digest** — :func:`artifact_digest` returns a stable SHA-256 over the
  canonical (sorted-key) JSON of the manifest plus the source text. Same input →
  same digest, across machines and runs. The digest is the artifact's id in the
  registry; re-uploading identical bytes is a no-op.

* **Signatures** — :class:`Signer` / :func:`verify_signature` implement an
  HMAC-SHA256 detached signature over the content digest, keyed per publisher.
  This is a *symmetric* scheme suitable for a first-party marketplace where the
  registry holds the publisher keys; the API surface (``sign`` / ``verify``) is
  identical to what an asymmetric (Ed25519) upgrade would expose, so swapping in
  public-key crypto later is a drop-in replacement behind :class:`Signer`.

Verification is constant-time (``hmac.compare_digest``) and *fail-closed*: a
missing, malformed, or mismatched signature raises
:class:`~app.platform.plugins.errors.SignatureError`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any

from app.platform.plugins.errors import SignatureError

#: The signature scheme identifier embedded in every :class:`Signature`.
SCHEME_HMAC_SHA256 = "hmac-sha256"


def canonical_json(data: Any) -> bytes:
    """Deterministic UTF-8 JSON: sorted keys, no insignificant whitespace."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def artifact_digest(manifest: dict[str, Any], source: str) -> str:
    """Stable SHA-256 hex digest over the canonical manifest + source bytes.

    The source is length-prefixed so manifest/source boundaries cannot be
    ambiguated (no concatenation-collision between different splits).
    """
    h = hashlib.sha256()
    h.update(b"kinora-plugin-v1\n")
    manifest_bytes = canonical_json(manifest)
    h.update(len(manifest_bytes).to_bytes(8, "big"))
    h.update(manifest_bytes)
    source_bytes = source.encode("utf-8")
    h.update(len(source_bytes).to_bytes(8, "big"))
    h.update(source_bytes)
    return h.hexdigest()


@dataclass(frozen=True, slots=True)
class Signature:
    """A detached signature over an artifact digest."""

    scheme: str
    key_id: str
    digest: str
    value: str  # hex MAC

    def to_dict(self) -> dict[str, str]:
        return {
            "scheme": self.scheme,
            "key_id": self.key_id,
            "digest": self.digest,
            "value": self.value,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Signature:
        if not isinstance(data, dict):
            raise SignatureError("signature payload is missing or not an object")
        try:
            return cls(
                scheme=str(data["scheme"]),
                key_id=str(data["key_id"]),
                digest=str(data["digest"]),
                value=str(data["value"]),
            )
        except KeyError as exc:
            raise SignatureError(f"signature missing field: {exc}") from exc


class Signer:
    """Signs/verifies artifact digests with a per-publisher HMAC key.

    In production the keys come from the host secret store; in tests they are
    supplied inline. The public surface (``sign``/``verify``) is the only thing
    the marketplace depends on, so the underlying scheme is swappable.
    """

    def __init__(self, keys: dict[str, bytes]) -> None:
        #: key_id -> secret key bytes.
        self._keys = dict(keys)

    def has_key(self, key_id: str) -> bool:
        return key_id in self._keys

    def sign(self, *, key_id: str, digest: str) -> Signature:
        """Produce a detached HMAC-SHA256 signature for ``digest``."""
        key = self._keys.get(key_id)
        if key is None:
            raise SignatureError(f"unknown signing key id: {key_id!r}")
        mac = hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()
        return Signature(scheme=SCHEME_HMAC_SHA256, key_id=key_id, digest=digest, value=mac)

    def verify(self, signature: Signature, *, expected_digest: str) -> None:
        """Verify ``signature`` against ``expected_digest`` (fail-closed)."""
        if signature.scheme != SCHEME_HMAC_SHA256:
            raise SignatureError(f"unsupported signature scheme: {signature.scheme!r}")
        if signature.digest != expected_digest:
            raise SignatureError("signed digest does not match the artifact content digest")
        key = self._keys.get(signature.key_id)
        if key is None:
            raise SignatureError(f"no key on file for signer {signature.key_id!r}")
        expected = hmac.new(key, expected_digest.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature.value):
            raise SignatureError("signature verification failed (digest tampered or wrong key)")


def verify_signature(
    signer: Signer, signature: Signature, *, manifest: dict[str, Any], source: str
) -> str:
    """Recompute the artifact digest and verify ``signature`` over it.

    Returns the verified digest on success; raises :class:`SignatureError`
    otherwise. This is the one call the marketplace uses at publish time.
    """
    digest = artifact_digest(manifest, source)
    signer.verify(signature, expected_digest=digest)
    return digest


__all__ = [
    "SCHEME_HMAC_SHA256",
    "Signature",
    "Signer",
    "artifact_digest",
    "canonical_json",
    "verify_signature",
]
