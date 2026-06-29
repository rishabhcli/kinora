"""The crypto provider: the single high-level entry point above the KMS seam.

:class:`CryptoProvider` is what every consumer (field encryption, the
SQLAlchemy type decorators, the tokenization vault, the rotation job) talks to.
It owns:

* the :class:`~app.zerotrust.crypto.keys.KeyManagementService` (default: the
  software KMS) used to generate/unwrap per-record DEKs;
* a small bounded LRU cache of *unwrapped* DEKs keyed by the wrapped bytes, so a
  hot record is not unwrapped on every column read (a real KMS round-trip is
  expensive). The cache holds bare key material in memory only — it is cleared
  on demand and never persisted;
* the :class:`AssociatedData` convention that binds record identity into every
  ciphertext.

The provider deliberately exposes two record-level primitives — :meth:`encrypt`
(fresh DEK per call) and :meth:`decrypt` — plus :meth:`derive_blind_index_key`
and :meth:`deterministic_key` so the searchable-encryption layers share one key
hierarchy instead of inventing their own.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
from collections import OrderedDict
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.zerotrust.crypto import aead
from app.zerotrust.crypto.aead import Algorithm
from app.zerotrust.crypto.keys import KeyManagementService, WrappedDek

#: Domain-separation labels for the auxiliary keys derived per data key. Each
#: maps a (DEK-equivalent) secret to a distinct purpose so, e.g., a leaked blind
#: index key cannot be used to decrypt or forge deterministic ciphertext.
_BLIND_INDEX_LABEL = b"kinora/zerotrust/blind-index"
_DETERMINISTIC_LABEL = b"kinora/zerotrust/deterministic"


@dataclass(frozen=True, slots=True)
class AssociatedData:
    """The record-identity tuple bound into every field ciphertext as AEAD AAD.

    Binding ``(table, column, record_id)`` means a ciphertext authenticated for
    one cell cannot be cut-and-pasted into another row or column without failing
    decryption — a confused-deputy / record-swap defence that comes for free from
    the AEAD tag.
    """

    table: str
    column: str
    record_id: str

    def to_bytes(self) -> bytes:
        """A canonical, unambiguous byte encoding (length-free fields joined by NUL)."""
        return b"\x00".join(
            part.encode("utf-8") for part in (self.table, self.column, self.record_id)
        )


@dataclass(frozen=True, slots=True)
class Ciphertext:
    """An encrypted field value: the AEAD envelope + the wrapped DEK that opens it.

    Both halves must be stored together; the wrapped DEK is opaque without the
    KMS, and the envelope is opaque without the DEK. The pair is what the
    SQLAlchemy ``EncryptedType`` serialises into a single column.
    """

    envelope: bytes
    wrapped_dek: WrappedDek


class CryptoProvider:
    """High-level encrypt/decrypt over the envelope hierarchy, with a DEK cache."""

    def __init__(
        self,
        kms: KeyManagementService,
        *,
        kek_id: str = "default",
        dek_cache_size: int = 1024,
    ) -> None:
        self._kms = kms
        self._kek_id = kek_id
        self._cache_size = max(0, dek_cache_size)
        self._cache: OrderedDict[bytes, bytes] = OrderedDict()
        #: Column-search seeds, cached per KEK (invalidated on KEK rotation).
        self._search_seeds: dict[str, bytes] = {}
        self._lock = threading.Lock()

    @property
    def kek_id(self) -> str:
        """The default KEK id new DEKs are wrapped under."""
        return self._kek_id

    # -- record-level field encryption -------------------------------------- #

    def encrypt(
        self,
        plaintext: bytes,
        aad: AssociatedData,
        *,
        algorithm: Algorithm = Algorithm.AES_256_GCM,
        kek_id: str | None = None,
    ) -> Ciphertext:
        """Encrypt ``plaintext`` under a fresh per-record DEK wrapped by the KEK."""
        data_key = self._kms.generate_data_key(kek_id or self._kek_id, algorithm=algorithm)
        try:
            envelope = aead.seal(
                data_key.plaintext, plaintext, aad=aad.to_bytes(), algorithm=algorithm
            )
        finally:
            # Cache the bare DEK so an immediate read-after-write is free, then
            # let the local reference drop.
            self._cache_put(data_key.wrapped.ciphertext, data_key.plaintext)
        return Ciphertext(envelope=envelope, wrapped_dek=data_key.wrapped)

    def decrypt(self, ciphertext: Ciphertext, aad: AssociatedData) -> bytes:
        """Unwrap the DEK (via cache or KMS) and open the envelope under it."""
        dek = self._dek_for(ciphertext.wrapped_dek)
        return aead.open_(dek, ciphertext.envelope, aad=aad.to_bytes())

    def rewrap(self, wrapped: WrappedDek) -> WrappedDek:
        """Re-wrap a DEK under the active KEK version (used by KEK rotation)."""
        new = self._kms.rewrap_data_key(wrapped)
        # The cached bare DEK (if any) is unchanged by re-wrap; re-key the cache.
        with self._lock:
            dek = self._cache.pop(wrapped.ciphertext, None)
            if dek is not None and self._cache_size:
                self._cache[new.ciphertext] = dek
        return new

    # -- auxiliary keys for searchable encryption --------------------------- #

    def derive_blind_index_key(self, dek: bytes) -> bytes:
        """Derive the HMAC key for blind indexes from a record/column secret."""
        return self._hkdf(dek, _BLIND_INDEX_LABEL)

    def deterministic_key(self, dek: bytes) -> bytes:
        """Derive the AES-GCM-SIV key used for deterministic (searchable) encryption."""
        return self._hkdf(dek, _DETERMINISTIC_LABEL)

    def column_search_seed(self, kek_id: str | None = None) -> bytes:
        """Return the column-stable 32-byte secret search keys are derived from.

        Delegates to the KMS :meth:`derive_purpose_key`, so the seed is identical
        for every record in a column (equality search depends on that) and is
        bound to the KEK's active version (so a KEK rotation rolls search keys
        too). Cached per-KEK in process — the value only changes on KEK rotation.
        """
        kid = kek_id or self._kek_id
        with self._lock:
            cached = self._search_seeds.get(kid)
            if cached is not None:
                return cached
        seed = self._kms.derive_purpose_key(kid, b"column-search")
        with self._lock:
            self._search_seeds[kid] = seed
        return seed

    @staticmethod
    def blind_index(key: bytes, value: bytes) -> bytes:
        """Compute a keyed, irreversible index token for ``value`` (HMAC-SHA256)."""
        return hmac.new(key, value, hashlib.sha256).digest()

    # -- internals ----------------------------------------------------------- #

    def _dek_for(self, wrapped: WrappedDek) -> bytes:
        cached = self._cache_get(wrapped.ciphertext)
        if cached is not None:
            return cached
        dek = self._kms.unwrap_data_key(wrapped)
        self._cache_put(wrapped.ciphertext, dek)
        return dek

    def _cache_get(self, key: bytes) -> bytes | None:
        if not self._cache_size:
            return None
        with self._lock:
            value = self._cache.get(key)
            if value is not None:
                self._cache.move_to_end(key)
            return value

    def _cache_put(self, key: bytes, value: bytes) -> None:
        if not self._cache_size:
            return
        with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)

    def invalidate_search_seeds(self) -> None:
        """Forget cached column-search seeds (call after a KEK rotation).

        Search keys are bound to the KEK's active version; once a KEK rotates, the
        next :meth:`column_search_seed` must re-derive against the new version so
        freshly written rows index under the rolled key. Existing rows are
        re-indexed by the rotation job.
        """
        with self._lock:
            self._search_seeds.clear()

    def clear_cache(self) -> None:
        """Drop all cached bare DEKs + search seeds from memory (security event)."""
        with self._lock:
            self._cache.clear()
            self._search_seeds.clear()

    @staticmethod
    def _hkdf(secret: bytes, label: bytes) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(), length=aead.KEY_LEN, salt=None, info=label
        ).derive(secret)


__all__ = ["AssociatedData", "Ciphertext", "CryptoProvider"]
