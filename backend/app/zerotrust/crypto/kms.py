"""A software Key Management Service implementing the facet-A KMS contract.

This is the in-process / development KMS. It satisfies
:class:`~app.zerotrust.crypto.keys.KeyManagementService` exactly, so a real
facet-A KMS (HSM, cloud KMS) can replace it without any change above this seam.

Key derivation
--------------
All KEKs are *derived* from a single 32-byte **root key** via HKDF-SHA256, so the
only secret an operator must provision is the root key (and in production the
root key never even reaches this process — it lives in the HSM). A KEK's bytes
are::

    KEK(kek_id, version) = HKDF(root, info = b"kinora/kek/" + kek_id + "/v" + version)

Deriving rather than storing KEKs means:

* a fixed root key makes the whole hierarchy **deterministic** — essential for
  the crypto-correctness and rotation tests, which assert exact ciphertext/round
  trips under known keys;
* **rotation** is just bumping the active version integer (a new KEK derives for
  free); no new key material has to be stored, only the wrapped DEKs re-wrapped.

The root key itself can be rotated by re-keying (out of scope of the per-record
DEK/KEK rotation that ``rotation.py`` automates online).

The bare-DEK material returned by :meth:`generate_data_key` is the only secret
that crosses the seam, exactly as the contract intends; wrapping/unwrapping all
happen here, under derived KEK bytes that never leave this object.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.zerotrust.crypto import aead
from app.zerotrust.crypto.aead import Algorithm, generate_key
from app.zerotrust.crypto.errors import KeyNotFoundError, KeyStateError
from app.zerotrust.crypto.keys import DataKey, KeyState, WrappedDek

#: HKDF info-string namespace so derived KEK bytes can never collide with any
#: other use of the root key (domain separation).
_KEK_INFO_PREFIX = b"kinora/zerotrust/kek/"


@dataclass
class _KekEntry:
    """In-memory KEK record: the active version + each version's lifecycle state."""

    active: int
    versions: dict[int, KeyState] = field(default_factory=dict)


class SoftwareKMS:
    """An HKDF-over-a-root-key KMS for development, tests, and self-hosting.

    Thread-safe: the KEK registry is guarded by a lock, since the KMS is a shared
    singleton consumed by concurrent request handlers and rotation workers.
    """

    def __init__(self, root_key: bytes) -> None:
        if len(root_key) != aead.KEY_LEN:
            raise KeyStateError(
                f"KMS root key must be {aead.KEY_LEN} bytes; got {len(root_key)}"
            )
        self._root = root_key
        self._lock = threading.RLock()
        self._keks: dict[str, _KekEntry] = {}

    # -- key administration -------------------------------------------------- #

    def register_kek(self, kek_id: str) -> None:
        """Create a KEK at version 1 (active, ENABLED). Idempotent."""
        with self._lock:
            if kek_id not in self._keks:
                self._keks[kek_id] = _KekEntry(active=1, versions={1: KeyState.ENABLED})

    def rotate_kek(self, kek_id: str) -> int:
        """Publish the next KEK version and make it active. Returns the new version.

        The previous version stays decrypt-capable (PENDING_DELETION) so existing
        DEKs can be drained/re-wrapped online before it is destroyed.
        """
        with self._lock:
            entry = self._require_kek(kek_id)
            old = entry.active
            new = max(entry.versions) + 1
            entry.versions[new] = KeyState.ENABLED
            # Demote the old active version to decrypt-only; rotation drains it.
            if entry.versions.get(old) == KeyState.ENABLED:
                entry.versions[old] = KeyState.PENDING_DELETION
            entry.active = new
            return new

    def active_version(self, kek_id: str) -> int:
        """Return the currently active (encrypting) version of ``kek_id``."""
        with self._lock:
            return self._require_kek(kek_id).active

    def set_state(self, kek_id: str, version: int, state: KeyState) -> None:
        """Force a KEK version into ``state`` (incident response / destruction)."""
        with self._lock:
            entry = self._require_kek(kek_id)
            if version not in entry.versions:
                raise KeyNotFoundError(f"KEK {kek_id!r} has no version {version}")
            entry.versions[version] = state

    # -- the KMS contract ---------------------------------------------------- #

    def generate_data_key(
        self, kek_id: str, *, algorithm: Algorithm = Algorithm.AES_256_GCM
    ) -> DataKey:
        """Generate a fresh DEK and wrap it under the active version of ``kek_id``."""
        with self._lock:
            entry = self._require_kek(kek_id)
            version = entry.active
            self._assert_can_encrypt(kek_id, entry, version)
        dek = generate_key()
        wrapped = self._wrap(kek_id, version, dek, algorithm)
        return DataKey(plaintext=dek, wrapped=wrapped)

    def unwrap_data_key(self, wrapped: WrappedDek) -> bytes:
        """Recover the bare DEK; honour the wrapping version's decrypt permission."""
        with self._lock:
            entry = self._require_kek(wrapped.kek_id)
            self._assert_can_decrypt(wrapped.kek_id, entry, wrapped.kek_version)
        kek = self._derive_kek(wrapped.kek_id, wrapped.kek_version)
        # AAD binds the wrap to its (kek_id, version) so a wrapped DEK cannot be
        # replayed as if it belonged to a different KEK.
        aad = self._wrap_aad(wrapped.kek_id, wrapped.kek_version)
        return aead.open_(kek, wrapped.ciphertext, aad=aad)

    def derive_purpose_key(self, kek_id: str, purpose: bytes, *, length: int = 32) -> bytes:
        """Deterministically derive a KEK-bound secret for a named purpose.

        Bound to the KEK's *active version* so rotating the KEK also rotates every
        purpose key derived from it (search keys roll forward with the KEK). The
        rotation job re-derives + re-indexes affected columns as part of a KEK
        rotation drain.
        """
        with self._lock:
            entry = self._require_kek(kek_id)
            version = entry.active
            self._assert_can_decrypt(kek_id, entry, version)
        kek = self._derive_kek(kek_id, version)
        info = b"kinora/zerotrust/purpose/" + purpose
        return HKDF(
            algorithm=hashes.SHA256(), length=length, salt=None, info=info
        ).derive(kek)

    def rewrap_data_key(self, wrapped: WrappedDek) -> WrappedDek:
        """Re-wrap a DEK under the active KEK version (idempotent if already current)."""
        with self._lock:
            entry = self._require_kek(wrapped.kek_id)
            target = entry.active
            self._assert_can_decrypt(wrapped.kek_id, entry, wrapped.kek_version)
            self._assert_can_encrypt(wrapped.kek_id, entry, target)
        if wrapped.kek_version == target:
            return wrapped
        dek = self.unwrap_data_key(wrapped)
        return self._wrap(wrapped.kek_id, target, dek, wrapped.algorithm)

    # -- internals ----------------------------------------------------------- #

    def _wrap(
        self, kek_id: str, version: int, dek: bytes, algorithm: Algorithm
    ) -> WrappedDek:
        kek = self._derive_kek(kek_id, version)
        ciphertext = aead.seal(kek, dek, aad=self._wrap_aad(kek_id, version))
        return WrappedDek(
            kek_id=kek_id, kek_version=version, ciphertext=ciphertext, algorithm=algorithm
        )

    def _derive_kek(self, kek_id: str, version: int) -> bytes:
        """Derive the 32-byte KEK for ``(kek_id, version)`` from the root key."""
        info = self._wrap_aad(kek_id, version)
        return HKDF(
            algorithm=hashes.SHA256(), length=aead.KEY_LEN, salt=None, info=info
        ).derive(self._root)

    @staticmethod
    def _wrap_aad(kek_id: str, version: int) -> bytes:
        return (
            _KEK_INFO_PREFIX
            + kek_id.encode("utf-8")
            + b"/v"
            + str(version).encode("ascii")
        )

    def _require_kek(self, kek_id: str) -> _KekEntry:
        entry = self._keks.get(kek_id)
        if entry is None:
            raise KeyNotFoundError(f"unknown KEK: {kek_id!r}")
        return entry

    def _assert_can_encrypt(self, kek_id: str, entry: _KekEntry, version: int) -> None:
        state = entry.versions.get(version)
        if state is None:
            raise KeyNotFoundError(f"KEK {kek_id!r} has no version {version}")
        if not state.can_encrypt:
            raise KeyStateError(
                f"KEK {kek_id!r} v{version} is {state.value}; cannot encrypt"
            )

    def _assert_can_decrypt(self, kek_id: str, entry: _KekEntry, version: int) -> None:
        state = entry.versions.get(version)
        if state is None:
            raise KeyNotFoundError(f"KEK {kek_id!r} has no version {version}")
        if not state.can_decrypt:
            raise KeyStateError(
                f"KEK {kek_id!r} v{version} is {state.value}; cannot decrypt"
            )


def from_env_root_key(*, env_var: str = "KINORA_KMS_ROOT_KEY") -> SoftwareKMS:
    """Build a :class:`SoftwareKMS` from a base64/hex root key in the environment.

    Falls back to a process-ephemeral random root key when the variable is unset
    (development convenience). A random root means data written this process is
    unreadable next boot — fine for a dev loop, never for production, where facet
    A's real KMS is wired instead.
    """
    raw = os.environ.get(env_var)
    if not raw:
        return SoftwareKMS(os.urandom(aead.KEY_LEN))
    return SoftwareKMS(_decode_key(raw))


def _decode_key(raw: str) -> bytes:
    """Decode a 32-byte key supplied as 64 hex chars or base64."""
    import base64
    import binascii

    text = raw.strip()
    try:
        if len(text) == 64:
            return binascii.unhexlify(text)
        return base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KeyStateError("KMS root key is not valid hex or base64") from exc


__all__ = ["SoftwareKMS", "from_env_root_key"]
