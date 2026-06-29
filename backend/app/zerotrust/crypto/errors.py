"""Exception hierarchy for the data-at-rest crypto facet.

Every failure raised by this facet derives from :class:`CryptoError` so callers
can catch the whole subsystem with one ``except``. The split between
:class:`CryptoConfigError` (a programming/deployment mistake — wrong key length,
unknown algorithm, missing KMS) and :class:`DecryptionError` (a runtime integrity
failure — wrong key, tampered ciphertext, AAD mismatch) lets the application
distinguish "fix your config" from "this ciphertext cannot be trusted".

Security note: :class:`DecryptionError` is deliberately opaque. It never echoes
plaintext, key bytes, or which check failed, because a precise error is an
oracle. AEAD tag failures and AAD mismatches collapse to the same message.
"""

from __future__ import annotations


class CryptoError(Exception):
    """Base class for every error raised by ``app.zerotrust.crypto``."""


class CryptoConfigError(CryptoError):
    """A misconfiguration: bad key length, unknown algorithm, missing provider.

    Raised eagerly (at construction / wiring time where possible) so deployment
    mistakes fail fast rather than corrupting data at write time.
    """


class EncryptionError(CryptoError):
    """Encryption could not be performed (e.g. KMS wrap failed)."""


class DecryptionError(CryptoError):
    """Authenticated decryption failed — the ciphertext is not trustworthy.

    This is raised for *any* integrity failure: a wrong key, a corrupted or
    truncated envelope, a forged AEAD tag, or an AAD (associated-data) mismatch.
    The message is intentionally generic to avoid acting as a padding/validity
    oracle.
    """


class KeyNotFoundError(CryptoError):
    """A referenced key (KEK or DEK) does not exist in the keyring/KMS."""


class KeyStateError(CryptoError):
    """A key exists but is in a state that forbids the requested operation.

    For example: encrypting under a key that has been *disabled* or *destroyed*,
    or wrapping under a key that is only authorised for *decrypt* during a
    rotation drain window.
    """


class TokenizationError(CryptoError):
    """A tokenization-vault operation failed (e.g. unknown token, bad format)."""


class AuthorizationError(CryptoError):
    """The caller is not authorised for a privileged operation (e.g. detokenize).

    Detokenization reveals real PII, so the vault refuses it unless the caller
    presents a purpose the token's policy permits. Authorisation is enforced
    here at the crypto layer, independent of any HTTP-layer auth.
    """


__all__ = [
    "AuthorizationError",
    "CryptoConfigError",
    "CryptoError",
    "DecryptionError",
    "EncryptionError",
    "KeyNotFoundError",
    "KeyStateError",
    "TokenizationError",
]
