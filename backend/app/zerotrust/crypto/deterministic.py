"""Deterministic (searchable) encryption for equality lookups.

A randomised AEAD ciphertext is unsearchable: the same plaintext encrypts to a
different value every time, so ``WHERE email = encrypt('a@b.com')`` can never
match. Deterministic encryption fixes this for *equality* — equal plaintexts
under one key produce identical ciphertexts — at a known, bounded privacy cost
(an attacker can see *which rows share a value*, though not the value itself).

How it is made safe
--------------------
Plain AES-GCM with a derived nonce is catastrophic: any nonce reuse across two
distinct plaintexts leaks the GCM authentication key. We instead use
**AES-256-GCM-SIV** (nonce-misuse-resistant) and derive the "nonce" (the SIV)
deterministically from the plaintext via HMAC, under a key separate from the
encryption key. The result:

* equal plaintext  → equal SIV → equal ciphertext (searchable);
* distinct plaintext → distinct SIV with overwhelming probability; and even on
  the astronomically unlikely SIV collision, GCM-SIV degrades gracefully (it
  leaks only equality of those two messages, never the key).

This layer is for **low-to-moderate-cardinality, exact-match** columns (email,
national id, a status code). Use a randomised :class:`EncryptedType` for free
text, and :mod:`blind_index` for range/prefix queries.
"""

from __future__ import annotations

import hashlib
import hmac

from app.zerotrust.crypto import aead
from app.zerotrust.crypto.aead import Algorithm

#: Domain-separation label for the SIV-derivation HMAC (distinct from the key
#: used to encrypt, so seeing a SIV reveals nothing about the encryption key).
_SIV_LABEL = b"kinora/zerotrust/deterministic/siv\x00"


def _derive_siv(siv_key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Derive the 12-byte synthetic IV deterministically from the plaintext.

    The AAD is folded in so two equal plaintexts in *different* contexts (e.g.
    different columns) still get different SIVs and therefore different
    ciphertexts — equality search stays scoped to one logical column.
    """
    mac = hmac.new(siv_key, _SIV_LABEL + aad + b"\x00" + plaintext, hashlib.sha256)
    return mac.digest()[: aead.NONCE_LEN]


def encrypt_deterministic(
    enc_key: bytes, siv_key: bytes, plaintext: bytes, *, aad: bytes = b""
) -> bytes:
    """Deterministically encrypt ``plaintext``; equal inputs yield equal outputs.

    Args:
        enc_key: the 32-byte AES-GCM-SIV encryption key.
        siv_key: a *separate* 32-byte key for SIV derivation (domain separated
            from ``enc_key`` upstream so neither reveals the other).
        plaintext: the value to encrypt (e.g. a normalised email).
        aad: associated data — also scopes the determinism (see :func:`_derive_siv`).

    Returns:
        A serialised AEAD envelope. Stored verbatim; an equality query encrypts
        the probe value the same way and compares bytes.
    """
    siv = _derive_siv(siv_key, plaintext, aad)
    return aead.seal(
        enc_key, plaintext, aad=aad, algorithm=Algorithm.AES_256_GCM_SIV, nonce=siv
    )


def decrypt_deterministic(enc_key: bytes, blob: bytes, *, aad: bytes = b"") -> bytes:
    """Decrypt a deterministic ciphertext (authenticated, like any envelope)."""
    return aead.open_(enc_key, blob, aad=aad)


__all__ = ["decrypt_deterministic", "encrypt_deterministic"]
