"""KMS abstraction + envelope encryption (the DEK/KEK key hierarchy).

A real Kinora deployment encrypts the canon vault, cloned-voice models, and
provider secrets at rest. Doing that *well* means **envelope encryption**: data
is encrypted under a per-payload **data encryption key (DEK)**; the DEK is itself
encrypted ("wrapped") under a long-lived **key encryption key (KEK)** that never
leaves the KMS. You store the ciphertext alongside the wrapped DEK; to read, you
ask the KMS to unwrap the DEK, then decrypt locally.

This module provides:

* :class:`KeyManagementService` — a :class:`Protocol` (the seam sibling facets and
  the storage layer depend on), with operations ``generate_data_key`` (returns a
  plaintext DEK + its wrapped form), ``encrypt``/``decrypt`` (wrap/unwrap a DEK or
  small blob directly under a KEK), ``rotate_key`` (mint a new KEK version),
  ``rewrap`` (re-wrap a ciphertext from an old version to the current one without
  exposing plaintext), and key-state lifecycle.
* :class:`LocalKms` — a deterministic, in-process implementation. KEKs are AES-256
  keys held in memory; wrapping uses **AES-256-GCM** with a random 96-bit nonce
  and the key id + version bound in as **additional authenticated data**, so a
  ciphertext minted under one key/version cannot be unwrapped under another (it
  fails authentication). Versioning is monotonic; rotation never deletes old
  versions (so existing ciphertext keeps decrypting) but re-points "current".
* :class:`EnvelopeCipher` — the convenience wrapper that does the full
  generate-DEK → AES-GCM-the-payload → emit (ciphertext + wrapped-DEK) dance and
  its inverse.

Determinism note: GCM nonces are random, so two encryptions of the same plaintext
differ — tests assert **round-trip** (decrypt(encrypt(x)) == x) and **tamper
rejection**, never ciphertext byte-equality. A KMS can be seeded from fixed key
bytes (:meth:`LocalKms.with_key`) for reproducible KEK material.
"""

from __future__ import annotations

import enum
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.zerotrust.identity.clock import Clock, SystemClock
from app.zerotrust.identity.errors import (
    DecryptionError,
    KeyDisabledError,
    KeyNotFoundError,
    KeyStateError,
    KmsError,
)

#: AES-256 key length in bytes.
_KEK_BYTES = 32
#: AES-GCM nonce length in bytes (96 bits — the GCM-recommended size).
_NONCE_BYTES = 12


class KeyState(enum.StrEnum):
    """Lifecycle state of a KEK version."""

    ENABLED = "enabled"
    DISABLED = "disabled"
    DESTROYED = "destroyed"


@dataclass(frozen=True, slots=True)
class WrappedKey:
    """An opaque, self-describing wrapped blob (a DEK or a small payload).

    Carries everything ``decrypt`` needs to find the right key version: the key
    id, the version it was wrapped under, the nonce, and the GCM ciphertext+tag.
    Serialisable to a compact, storage-friendly string.
    """

    key_id: str
    version: int
    nonce: bytes
    ciphertext: bytes

    def to_token(self) -> str:
        """Compact, URL-safe serialisation for storage next to the data."""

        import base64

        payload = {
            "k": self.key_id,
            "v": self.version,
            "n": base64.urlsafe_b64encode(self.nonce).decode("ascii"),
            "c": base64.urlsafe_b64encode(self.ciphertext).decode("ascii"),
        }
        return base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode()
        ).decode("ascii")

    @classmethod
    def from_token(cls, token: str) -> WrappedKey:
        import base64

        try:
            payload = json.loads(base64.urlsafe_b64decode(token.encode()))
            return cls(
                key_id=payload["k"],
                version=int(payload["v"]),
                nonce=base64.urlsafe_b64decode(payload["n"]),
                ciphertext=base64.urlsafe_b64decode(payload["c"]),
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise KmsError("malformed wrapped-key token") from exc


@dataclass(slots=True)
class DataKey:
    """A generated DEK: plaintext (use then forget) + its wrapped form."""

    plaintext: bytes
    wrapped: WrappedKey

    def for_aesgcm(self) -> AESGCM:
        return AESGCM(self.plaintext)


@dataclass(slots=True)
class _KeyVersion:
    version: int
    material: bytes
    state: KeyState
    created_at: datetime


@dataclass(slots=True)
class _ManagedKey:
    key_id: str
    current_version: int
    versions: dict[int, _KeyVersion] = field(default_factory=dict)


@runtime_checkable
class KeyManagementService(Protocol):
    """The KMS seam consumed by storage + sibling zero-trust facets."""

    def create_key(self, key_id: str) -> int: ...  # pragma: no cover
    def rotate_key(self, key_id: str) -> int: ...  # pragma: no cover
    def encrypt(  # pragma: no cover
        self, key_id: str, plaintext: bytes, *, aad: bytes = b""
    ) -> WrappedKey: ...
    def decrypt(self, wrapped: WrappedKey, *, aad: bytes = b"") -> bytes: ...  # pragma: no cover
    def generate_data_key(  # pragma: no cover
        self, key_id: str, *, length: int = 32
    ) -> DataKey: ...
    def rewrap(  # pragma: no cover
        self, wrapped: WrappedKey, *, aad: bytes = b""
    ) -> WrappedKey: ...


@dataclass(slots=True)
class LocalKms:
    """An in-process KMS: KEKs in memory, AES-256-GCM wrap, monotonic versions."""

    clock: Clock = field(default_factory=SystemClock)
    _keys: dict[str, _ManagedKey] = field(default_factory=dict)

    # -- key lifecycle ----------------------------------------------------- #
    def create_key(self, key_id: str, *, material: bytes | None = None) -> int:
        """Create a KEK named *key_id* (version 1). Returns the version."""

        if key_id in self._keys:
            raise KmsError(f"key {key_id!r} already exists")
        if material is not None and len(material) != _KEK_BYTES:
            raise KmsError("KEK material must be 32 bytes (AES-256)")
        version = _KeyVersion(
            version=1,
            material=material or os.urandom(_KEK_BYTES),
            state=KeyState.ENABLED,
            created_at=self.clock.now(),
        )
        self._keys[key_id] = _ManagedKey(key_id, 1, {1: version})
        return 1

    @classmethod
    def with_key(cls, key_id: str, material: bytes, *, clock: Clock | None = None) -> LocalKms:
        """Construct a KMS pre-seeded with one fixed-material KEK (test helper)."""

        kms = cls(clock=clock or SystemClock())
        kms.create_key(key_id, material=material)
        return kms

    def rotate_key(self, key_id: str, *, material: bytes | None = None) -> int:
        """Mint a new KEK version and make it current. Old versions stay usable."""

        managed = self._require_key(key_id)
        if material is not None and len(material) != _KEK_BYTES:
            raise KmsError("KEK material must be 32 bytes (AES-256)")
        new_version = max(managed.versions) + 1
        managed.versions[new_version] = _KeyVersion(
            version=new_version,
            material=material or os.urandom(_KEK_BYTES),
            state=KeyState.ENABLED,
            created_at=self.clock.now(),
        )
        managed.current_version = new_version
        return new_version

    def disable_version(self, key_id: str, version: int) -> None:
        """Disable a version: still findable, but unusable for wrap/unwrap."""

        kv = self._require_version(key_id, version)
        if kv.state is KeyState.DESTROYED:
            raise KeyStateError("cannot disable a destroyed key version")
        kv.state = KeyState.DISABLED

    def enable_version(self, key_id: str, version: int) -> None:
        kv = self._require_version(key_id, version)
        if kv.state is KeyState.DESTROYED:
            raise KeyStateError("cannot enable a destroyed key version")
        kv.state = KeyState.ENABLED

    def destroy_version(self, key_id: str, version: int) -> None:
        """Cryptographically destroy a version: zeroise material, mark destroyed."""

        managed = self._require_key(key_id)
        kv = self._require_version(key_id, version)
        if managed.current_version == version and len(_enabled(managed)) <= 1:
            raise KeyStateError("cannot destroy the only usable version")
        kv.material = b"\x00" * len(kv.material)
        kv.state = KeyState.DESTROYED

    def current_version(self, key_id: str) -> int:
        return self._require_key(key_id).current_version

    def versions(self, key_id: str) -> tuple[int, ...]:
        return tuple(sorted(self._require_key(key_id).versions))

    def key_ids(self) -> frozenset[str]:
        return frozenset(self._keys)

    # -- wrap / unwrap ----------------------------------------------------- #
    def encrypt(self, key_id: str, plaintext: bytes, *, aad: bytes = b"") -> WrappedKey:
        """Wrap *plaintext* under the current version of *key_id*."""

        managed = self._require_key(key_id)
        return self._wrap(managed, managed.current_version, plaintext, aad)

    def decrypt(self, wrapped: WrappedKey, *, aad: bytes = b"") -> bytes:
        """Unwrap a :class:`WrappedKey` under the version it names."""

        kv = self._require_version(wrapped.key_id, wrapped.version)
        if kv.state is KeyState.DESTROYED:
            raise KeyDisabledError(
                f"key {wrapped.key_id!r} v{wrapped.version} is destroyed"
            )
        if kv.state is KeyState.DISABLED:
            raise KeyDisabledError(
                f"key {wrapped.key_id!r} v{wrapped.version} is disabled"
            )
        aesgcm = AESGCM(kv.material)
        full_aad = self._aad(wrapped.key_id, wrapped.version, aad)
        try:
            return aesgcm.decrypt(wrapped.nonce, wrapped.ciphertext, full_aad)
        except InvalidTag as exc:
            raise DecryptionError(
                "authenticated decryption failed (tampered ciphertext, wrong key, "
                "or mismatched AAD)"
            ) from exc

    def generate_data_key(self, key_id: str, *, length: int = 32) -> DataKey:
        """Generate a fresh DEK and return it plaintext + wrapped under *key_id*."""

        if length not in (16, 24, 32):
            raise KmsError("DEK length must be 16, 24, or 32 bytes")
        dek = os.urandom(length)
        wrapped = self.encrypt(key_id, dek)
        return DataKey(plaintext=dek, wrapped=wrapped)

    def rewrap(self, wrapped: WrappedKey, *, aad: bytes = b"") -> WrappedKey:
        """Re-wrap *wrapped* from its version to the current one.

        Unwraps under the old version then wraps under the current version
        **without ever returning the plaintext to the caller** — the canonical
        KMS rotation primitive for re-encrypting stored DEKs after a KEK rotation.
        """

        plaintext = self.decrypt(wrapped, aad=aad)
        managed = self._require_key(wrapped.key_id)
        try:
            return self._wrap(managed, managed.current_version, plaintext, aad)
        finally:
            # best-effort scrub of the transient plaintext
            del plaintext

    def needs_rewrap(self, wrapped: WrappedKey) -> bool:
        """Whether *wrapped* is under an older-than-current key version."""

        return wrapped.version < self._require_key(wrapped.key_id).current_version

    # -- internals --------------------------------------------------------- #
    def _wrap(
        self, managed: _ManagedKey, version: int, plaintext: bytes, aad: bytes
    ) -> WrappedKey:
        kv = managed.versions[version]
        if kv.state is not KeyState.ENABLED:
            raise KeyDisabledError(
                f"key {managed.key_id!r} v{version} is not enabled for wrapping"
            )
        nonce = os.urandom(_NONCE_BYTES)
        aesgcm = AESGCM(kv.material)
        ct = aesgcm.encrypt(nonce, plaintext, self._aad(managed.key_id, version, aad))
        return WrappedKey(managed.key_id, version, nonce, ct)

    @staticmethod
    def _aad(key_id: str, version: int, extra: bytes) -> bytes:
        # Bind the key id + version (and any caller AAD) into the GCM tag so a
        # blob can't be unwrapped under a different key/version/context.
        return f"{key_id}:{version}:".encode() + extra

    def _require_key(self, key_id: str) -> _ManagedKey:
        managed = self._keys.get(key_id)
        if managed is None:
            raise KeyNotFoundError(f"no such key {key_id!r}")
        return managed

    def _require_version(self, key_id: str, version: int) -> _KeyVersion:
        managed = self._require_key(key_id)
        kv = managed.versions.get(version)
        if kv is None:
            raise KeyNotFoundError(f"no version {version} of key {key_id!r}")
        return kv


@dataclass(slots=True)
class EnvelopeCipher:
    """Full envelope encryption over a :class:`KeyManagementService`.

    ``seal`` → (ciphertext, wrapped-DEK); ``open_`` reverses it. The payload is
    encrypted with a one-shot DEK under AES-256-GCM; the DEK is wrapped by the
    KMS. Optional ``aad`` is bound into the **payload** GCM tag so context (e.g.
    the canon object id) is authenticated. The DEK wrap itself binds only the KMS
    key id + version (not the payload AAD), which keeps the wrapped DEK
    re-wrappable by a generic KEK-rotation sweep — :meth:`KeyManagementService.rewrap`
    has no way to know a caller's per-payload AAD, and the payload is already
    AAD-authenticated independently.
    """

    kms: KeyManagementService
    key_id: str

    def seal(self, plaintext: bytes, *, aad: bytes = b"") -> Envelope:
        data_key = self.kms.generate_data_key(self.key_id)
        nonce = os.urandom(_NONCE_BYTES)
        ct = data_key.for_aesgcm().encrypt(nonce, plaintext, aad or None)
        return Envelope(
            nonce=nonce, ciphertext=ct, wrapped_dek=data_key.wrapped, aad=aad
        )

    def open_(self, envelope: Envelope) -> bytes:
        dek = self.kms.decrypt(envelope.wrapped_dek)
        aesgcm = AESGCM(dek)
        try:
            return aesgcm.decrypt(
                envelope.nonce, envelope.ciphertext, envelope.aad or None
            )
        except InvalidTag as exc:
            raise DecryptionError("envelope payload failed authentication") from exc


@dataclass(frozen=True, slots=True)
class Envelope:
    """A sealed payload: nonce + ciphertext + the KMS-wrapped DEK."""

    nonce: bytes
    ciphertext: bytes
    wrapped_dek: WrappedKey
    aad: bytes = b""


def _enabled(managed: _ManagedKey) -> list[_KeyVersion]:
    return [v for v in managed.versions.values() if v.state is KeyState.ENABLED]


__all__ = [
    "DataKey",
    "Envelope",
    "EnvelopeCipher",
    "KeyManagementService",
    "KeyState",
    "LocalKms",
    "WrappedKey",
]
