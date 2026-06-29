"""Authenticated encryption with associated data (AEAD) primitives + wire format.

This module is the cryptographic floor of the data-at-rest facet. Everything
above it (field encryption, the tokenization vault, envelope key wrapping)
serialises through the single self-describing :class:`Envelope` format defined
here, so a ciphertext is always decodable without out-of-band metadata and the
*algorithm* and *format version* travel with the bytes.

Algorithms
----------
* :data:`Algorithm.AES_256_GCM` — the default. 96-bit random nonce, 128-bit tag.
* :data:`Algorithm.CHACHA20_POLY1305` — for platforms without AES-NI; same nonce
  and tag sizes.
* :data:`Algorithm.AES_256_GCM_SIV` — *nonce-misuse-resistant*. Used by the
  deterministic-encryption layer (``deterministic.py``) where the nonce is
  derived deterministically from the plaintext so equal plaintexts produce equal
  ciphertexts (enabling equality search) **without** the catastrophic
  key-recovery failure plain GCM suffers under nonce reuse.

Associated data (AAD)
---------------------
AAD is authenticated but not encrypted. Higher layers bind a record's *identity*
(table, column, primary key, key id) into the AAD so a ciphertext cannot be
silently copied to a different row/column — a "cut-and-paste" / confused-deputy
defence. A tampered AAD fails the tag check exactly like a tampered ciphertext.

Wire format (``Envelope.to_bytes``)
-----------------------------------
``MAGIC(2) | VERSION(1) | ALG(1) | NONCE_LEN(1) | nonce | ciphertext||tag``

The 4-byte fixed header plus the variable nonce is a tiny, constant overhead.
Parsing is strict: an unknown magic, version, or algorithm raises
:class:`~app.zerotrust.crypto.errors.DecryptionError` (never a silent guess).
"""

from __future__ import annotations

import enum
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import (
    AESGCM,
    AESGCMSIV,
    ChaCha20Poly1305,
)

from app.zerotrust.crypto.errors import CryptoConfigError, DecryptionError

#: 2-byte format magic ("KE" = Kinora Envelope). Lets a decoder reject bytes that
#: were never produced by this facet before doing any crypto work.
MAGIC = b"KE"

#: Bumped only on a backwards-incompatible wire-format change. Decoders accept
#: any version they know how to parse; today that is exactly ``1``.
ENVELOPE_VERSION = 1

#: Every supported algorithm uses a 96-bit (12-byte) nonce and a 128-bit tag.
NONCE_LEN = 12
TAG_LEN = 16
#: All three AEADs here are 256-bit-key ciphers.
KEY_LEN = 32


class Algorithm(enum.IntEnum):
    """AEAD algorithm identifiers, stored as a single byte in the envelope.

    Integer values are part of the on-disk wire format and must never be
    renumbered — only appended to.
    """

    AES_256_GCM = 1
    CHACHA20_POLY1305 = 2
    AES_256_GCM_SIV = 3


#: The default algorithm for fresh, randomised encryption.
DEFAULT_ALGORITHM = Algorithm.AES_256_GCM

#: Algorithms whose nonce may safely be derived deterministically from the
#: plaintext (misuse-resistant). Only AES-GCM-SIV qualifies; deriving a nonce for
#: plain GCM and reusing it across two distinct plaintexts under one key leaks the
#: authentication key, so the deterministic layer is restricted to this set.
NONCE_MISUSE_RESISTANT = frozenset({Algorithm.AES_256_GCM_SIV})


def _cipher(algorithm: Algorithm, key: bytes) -> AESGCM | AESGCMSIV | ChaCha20Poly1305:
    """Instantiate the ``cryptography`` AEAD object for ``algorithm``.

    Raises:
        CryptoConfigError: if the key length is wrong or the algorithm is unknown.
    """
    if len(key) != KEY_LEN:
        raise CryptoConfigError(
            f"AEAD key must be {KEY_LEN} bytes (256-bit); got {len(key)}"
        )
    if algorithm == Algorithm.AES_256_GCM:
        return AESGCM(key)
    if algorithm == Algorithm.CHACHA20_POLY1305:
        return ChaCha20Poly1305(key)
    if algorithm == Algorithm.AES_256_GCM_SIV:
        return AESGCMSIV(key)
    raise CryptoConfigError(f"unsupported AEAD algorithm: {algorithm!r}")


def generate_key() -> bytes:
    """Return a fresh 32-byte (256-bit) AEAD key from the OS CSPRNG."""
    return os.urandom(KEY_LEN)


@dataclass(frozen=True, slots=True)
class Envelope:
    """A parsed, self-describing AEAD ciphertext.

    Attributes:
        algorithm: which AEAD produced ``ciphertext`` (travels in the header).
        nonce: the per-message nonce (random, or deterministically derived).
        ciphertext: the AEAD output, *including* the appended authentication tag.
        version: the wire-format version (defaults to the current one).
    """

    algorithm: Algorithm
    nonce: bytes
    ciphertext: bytes
    version: int = ENVELOPE_VERSION

    def to_bytes(self) -> bytes:
        """Serialise to the compact ``MAGIC|VER|ALG|NLEN|nonce|ct`` layout."""
        return b"".join(
            (
                MAGIC,
                bytes((self.version, int(self.algorithm), len(self.nonce))),
                self.nonce,
                self.ciphertext,
            )
        )

    @classmethod
    def from_bytes(cls, blob: bytes) -> Envelope:
        """Parse a serialised envelope; raise :class:`DecryptionError` if malformed.

        Strict by design — a malformed or alien blob is an integrity failure, not
        a recoverable condition, and is reported as one (without leaking which
        check failed beyond "malformed envelope").
        """
        # 2 magic + 3 header + at least a nonce + a tag.
        if len(blob) < 5 + NONCE_LEN + TAG_LEN or blob[:2] != MAGIC:
            raise DecryptionError("malformed AEAD envelope")
        version = blob[2]
        if version != ENVELOPE_VERSION:
            raise DecryptionError(f"unsupported envelope version: {version}")
        try:
            algorithm = Algorithm(blob[3])
        except ValueError as exc:
            raise DecryptionError("unknown AEAD algorithm in envelope") from exc
        nonce_len = blob[4]
        nonce_start = 5
        ct_start = nonce_start + nonce_len
        # A short blob (e.g. truncated mid-nonce) must not silently pass.
        if nonce_len == 0 or len(blob) < ct_start + TAG_LEN:
            raise DecryptionError("truncated AEAD envelope")
        return cls(
            algorithm=algorithm,
            nonce=blob[nonce_start:ct_start],
            ciphertext=blob[ct_start:],
            version=version,
        )


def seal(
    key: bytes,
    plaintext: bytes,
    *,
    aad: bytes = b"",
    algorithm: Algorithm = DEFAULT_ALGORITHM,
    nonce: bytes | None = None,
) -> bytes:
    """Encrypt ``plaintext`` and return a serialised :class:`Envelope`.

    Args:
        key: a 32-byte AEAD key.
        plaintext: the bytes to protect.
        aad: associated data — authenticated but not encrypted. Higher layers
            bind record identity here so ciphertexts cannot be relocated.
        algorithm: which AEAD to use (default AES-256-GCM).
        nonce: a caller-supplied nonce. **Only** pass this for a
            nonce-misuse-resistant algorithm (the deterministic layer does, to
            make equal plaintexts encrypt identically). For randomised
            algorithms leave it ``None`` so a fresh random nonce is drawn — a
            supplied nonce under plain GCM is a footgun and is rejected.

    Raises:
        CryptoConfigError: bad key/nonce length, or a supplied nonce on a
            non-misuse-resistant algorithm.
    """
    if nonce is None:
        nonce = os.urandom(NONCE_LEN)
    else:
        if algorithm not in NONCE_MISUSE_RESISTANT:
            raise CryptoConfigError(
                "a caller-supplied nonce is only permitted for nonce-misuse-"
                f"resistant algorithms; {algorithm.name} is not one"
            )
        if len(nonce) != NONCE_LEN:
            raise CryptoConfigError(
                f"nonce must be {NONCE_LEN} bytes; got {len(nonce)}"
            )
    cipher = _cipher(algorithm, key)
    ciphertext = cipher.encrypt(nonce, plaintext, aad)
    return Envelope(algorithm=algorithm, nonce=nonce, ciphertext=ciphertext).to_bytes()


def open_(key: bytes, blob: bytes, *, aad: bytes = b"") -> bytes:
    """Decrypt a serialised :class:`Envelope`; return the plaintext.

    Args:
        key: the 32-byte AEAD key the envelope was sealed under.
        blob: the serialised envelope (output of :func:`seal`).
        aad: the associated data that was bound at seal time. A mismatch fails
            the authentication tag exactly like ciphertext tampering.

    Raises:
        DecryptionError: for any integrity failure (wrong key, tampered bytes,
            AAD mismatch, malformed envelope). Deliberately opaque.
    """
    envelope = Envelope.from_bytes(blob)
    cipher = _cipher(envelope.algorithm, key)
    try:
        return cipher.decrypt(envelope.nonce, envelope.ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError("authenticated decryption failed") from exc


__all__ = [
    "DEFAULT_ALGORITHM",
    "ENVELOPE_VERSION",
    "KEY_LEN",
    "MAGIC",
    "NONCE_LEN",
    "NONCE_MISUSE_RESISTANT",
    "TAG_LEN",
    "Algorithm",
    "Envelope",
    "generate_key",
    "open_",
    "seal",
]
