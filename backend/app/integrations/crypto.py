"""Seal/unseal the OAuth token blob so credentials are not stored in plaintext.

The :class:`~app.db.models.integration.AppConnection` token column holds access +
refresh tokens. Those must not sit in the database as readable plaintext. This
module provides a small symmetric seal:

* When ``cryptography`` is available and an ``INTEGRATIONS_ENCRYPTION_KEY`` is
  configured, it uses Fernet (AES-128-CBC + HMAC, authenticated) — the real seal.
* Otherwise it falls back to a keyed, reversible, **clearly-labelled** obfuscation
  so local dev / tests without a key still round-trip. The fallback is *not*
  cryptographically strong and is tagged ``v0:`` so it is auditable; production
  must set a key (the service logs a warning when the fallback is in use).

Either way the public contract is the same: :func:`seal` → opaque string,
:func:`unseal` → original JSON-able mapping.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from app.core.logging import get_logger

logger = get_logger("app.integrations.crypto")

_FERNET_PREFIX = "f1:"
_FALLBACK_PREFIX = "v0:"


class TokenSealer:
    """Seal/unseal small JSON-able mappings with a configured key.

    Construct once per process (the container holds it). ``key`` is the raw
    secret string from settings; ``None`` means "no real key configured" → the
    reversible fallback is used and a warning is logged once.
    """

    def __init__(self, key: str | None) -> None:
        self._key = key
        self._fernet = self._build_fernet(key)
        if self._fernet is None and key is None:
            logger.warning(
                "integrations.crypto.fallback",
                detail="INTEGRATIONS_ENCRYPTION_KEY not set; tokens use reversible obfuscation",
            )

    @staticmethod
    def _build_fernet(key: str | None) -> Any | None:
        if not key:
            return None
        try:
            from cryptography.fernet import Fernet
        except Exception:  # noqa: BLE001 - cryptography optional
            return None
        # Derive a stable 32-byte urlsafe-base64 Fernet key from any input string.
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

    @property
    def is_strong(self) -> bool:
        """True when real authenticated encryption (Fernet) is active."""
        return self._fernet is not None

    def seal(self, payload: dict[str, Any]) -> str:
        """Serialise + encrypt ``payload`` to an opaque, prefixed string."""
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if self._fernet is not None:
            token = self._fernet.encrypt(raw).decode("ascii")
            return _FERNET_PREFIX + token
        return _FALLBACK_PREFIX + self._xor(raw)

    def unseal(self, blob: str) -> dict[str, Any]:
        """Reverse :meth:`seal`. Raises ``ValueError`` on a malformed/forged blob."""
        if blob.startswith(_FERNET_PREFIX):
            if self._fernet is None:
                raise ValueError("encrypted token present but no encryption key configured")
            from cryptography.fernet import InvalidToken

            try:
                raw = self._fernet.decrypt(blob[len(_FERNET_PREFIX):].encode("ascii"))
            except InvalidToken as exc:
                raise ValueError("token failed authentication") from exc
            return self._loads(raw)
        if blob.startswith(_FALLBACK_PREFIX):
            raw = self._unxor(blob[len(_FALLBACK_PREFIX):])
            return self._loads(raw)
        raise ValueError("unrecognised token blob format")

    # -- reversible fallback (NOT strong; labelled v0:) --------------------- #
    def _keystream(self, n: int) -> bytes:
        seed = (self._key or "kinora-integrations-fallback").encode("utf-8")
        out = bytearray()
        counter = 0
        while len(out) < n:
            out += hmac.new(seed, counter.to_bytes(8, "big"), hashlib.sha256).digest()
            counter += 1
        return bytes(out[:n])

    def _xor(self, raw: bytes) -> str:
        ks = self._keystream(len(raw))
        masked = bytes(b ^ k for b, k in zip(raw, ks, strict=True))
        return base64.urlsafe_b64encode(masked).decode("ascii")

    def _unxor(self, blob: str) -> bytes:
        masked = base64.urlsafe_b64decode(blob.encode("ascii"))
        ks = self._keystream(len(masked))
        return bytes(b ^ k for b, k in zip(masked, ks, strict=True))

    @staticmethod
    def _loads(raw: bytes) -> dict[str, Any]:
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("sealed payload is not a JSON object")
        return value


__all__ = ["TokenSealer"]
