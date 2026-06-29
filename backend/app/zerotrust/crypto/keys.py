"""The KMS contract (facet-A seam) + the envelope key hierarchy.

Envelope encryption, three tiers
--------------------------------
::

    root master key  (never leaves the KMS / HSM boundary)
        │  wraps
        ▼
    KEK   key-encryption key      (one active per "purpose"; rotatable)
        │  wraps
        ▼
    DEK   data-encryption key     (one per record; cheap, disposable, the only
                                    key a ciphertext is actually sealed under)

A DEK is generated fresh per record, used to AEAD-seal the record's plaintext,
then *wrapped* (encrypted) by the active KEK and stored next to the ciphertext as
a :class:`WrappedDek`. To read, the wrapped DEK is sent back to the KMS, which
unwraps it under the KEK; the bare DEK lives only transiently in process memory.
This is what makes **rotation cheap**: rotating a KEK re-wraps DEKs (small,
fast) without touching the (large) ciphertext, and rotating/destroying a single
DEK affects exactly one record (crypto-shredding).

The KMS contract (``KeyManagementService``)
-------------------------------------------
Facet A owns the production KMS (HSM / cloud KMS). This facet only needs a narrow
seam — generate a DEK wrapped under a named KEK, and unwrap it — so the contract
is defined here as a :class:`typing.Protocol`. Any facet-A implementation that
satisfies it drops straight in; until then, ``kms.py`` ships a software KMS that
implements it for development and tests. The root key never crosses this seam:
wrap/unwrap happen *inside* the KMS.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.zerotrust.crypto.aead import Algorithm


class KeyState(enum.Enum):
    """Lifecycle state of a key (KEK or DEK) in the keyring.

    The state machine is monotone toward destruction: a key may move
    ENABLED → DISABLED → ENABLED, ENABLED/DISABLED → PENDING_DELETION →
    DESTROYED, but never back from DESTROYED.

    * ``ENABLED``  — usable for encrypt + decrypt.
    * ``DISABLED`` — temporarily unusable (incident response); reversible.
    * ``PENDING_DELETION`` — scheduled for destruction; **decrypt-only** so an
      in-flight rotation can still drain ciphertext off this key.
    * ``DESTROYED`` — key material is gone; any ciphertext under it is
      unrecoverable (intentional — this is how crypto-shredding works).
    """

    ENABLED = "enabled"
    DISABLED = "disabled"
    PENDING_DELETION = "pending_deletion"
    DESTROYED = "destroyed"

    @property
    def can_encrypt(self) -> bool:
        """True only when the key may seal/wrap new data."""
        return self is KeyState.ENABLED

    @property
    def can_decrypt(self) -> bool:
        """True while the key can still open/unwrap existing data."""
        return self in (KeyState.ENABLED, KeyState.DISABLED, KeyState.PENDING_DELETION)


@dataclass(frozen=True, slots=True)
class WrappedDek:
    """A data-encryption key encrypted ("wrapped") under a KEK.

    This is the *only* form of a DEK that is ever persisted. It is opaque outside
    the KMS: ``ciphertext`` is meaningless without the KEK, which never leaves the
    KMS boundary.

    Attributes:
        kek_id: which KEK wrapped this DEK (selects the unwrap key + lets a KEK
            rotation find every DEK that needs re-wrapping).
        kek_version: the KEK version used — a KEK is versioned so re-wrapping is
            an idempotent, auditable bump.
        ciphertext: the wrapped DEK bytes (a serialised AEAD envelope).
        algorithm: the AEAD the *unwrapped* DEK is meant to be used with. Carried
            so the data layer seals under the right cipher without a second lookup.
    """

    kek_id: str
    kek_version: int
    ciphertext: bytes
    algorithm: Algorithm = Algorithm.AES_256_GCM


@dataclass(frozen=True, slots=True)
class DataKey:
    """A freshly generated DEK in both plaintext and wrapped form.

    Returned by :meth:`KeyManagementService.generate_data_key`. The caller seals
    its record with :attr:`plaintext`, persists :attr:`wrapped`, and then should
    drop the plaintext reference promptly (it is held only as long as needed).
    """

    plaintext: bytes
    wrapped: WrappedDek


@runtime_checkable
class KeyManagementService(Protocol):
    """The narrow KMS seam consumed by the data-at-rest facet (owned by facet A).

    Implementations keep root/KEK material inside their own trust boundary; the
    only secrets that cross this interface are *transient* bare DEKs returned to
    the caller for immediate use. ``kek_id`` selects a logical key (e.g. one per
    data domain — ``pii``, ``books``); the KMS resolves its active version.
    """

    def generate_data_key(
        self, kek_id: str, *, algorithm: Algorithm = Algorithm.AES_256_GCM
    ) -> DataKey:
        """Generate a new DEK, returning it both bare and wrapped under ``kek_id``.

        Raises:
            KeyNotFoundError: ``kek_id`` is unknown.
            KeyStateError: the KEK cannot currently encrypt (disabled/destroyed).
        """
        ...

    def unwrap_data_key(self, wrapped: WrappedDek) -> bytes:
        """Recover the bare DEK from a :class:`WrappedDek`.

        Raises:
            KeyNotFoundError: the wrapping KEK/version no longer exists.
            KeyStateError: the KEK has been destroyed.
            DecryptionError: the wrapped bytes fail authentication.
        """
        ...

    def rewrap_data_key(self, wrapped: WrappedDek) -> WrappedDek:
        """Re-wrap a DEK under the *current active version* of its KEK.

        Used by the rotation job: unwrap under the old version (decrypt-allowed)
        and wrap under the new one, without ever exposing the DEK outside the KMS
        more than a normal unwrap would. Returns the DEK unchanged if it is
        already wrapped under the active version (idempotent).
        """
        ...

    def derive_purpose_key(self, kek_id: str, purpose: bytes, *, length: int = 32) -> bytes:
        """Derive a stable, KEK-bound secret for a named ``purpose``.

        Unlike :meth:`generate_data_key` (random, per-record), this is a
        *deterministic* derivation: the same ``(kek_id, purpose)`` always yields
        the same bytes for as long as the KEK's active version is unchanged. The
        searchable-encryption layers use it for **column-wide** search keys, which
        must be identical across every row of a column or equality search could
        never match. A real (cloud/HSM) KMS implements this as a key-derivation
        operation under the KEK; the derived bytes still leave the seam only as a
        transient secret, exactly like a DEK.
        """
        ...


__all__ = [
    "DataKey",
    "KeyManagementService",
    "KeyState",
    "WrappedDek",
]
