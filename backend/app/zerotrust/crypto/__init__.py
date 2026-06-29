"""Data-at-rest protection — application-layer encryption (zero-trust facet B).

The public surface, bottom-up:

Primitives & keys
    * :mod:`aead` — AES-256-GCM / ChaCha20-Poly1305 / AES-GCM-SIV with a versioned,
      self-describing :class:`~aead.Envelope` wire format and AAD binding.
    * :mod:`keys` — the **KMS contract** (:class:`~keys.KeyManagementService`,
      owned by sibling facet A) and the envelope key hierarchy (root → KEK → DEK).
    * :mod:`kms` — :class:`~kms.SoftwareKMS`, an HKDF-over-root-key implementation
      of the contract for development/tests (deterministic under a fixed root).
    * :mod:`context` — :class:`~context.CryptoProvider`: the high-level
      encrypt/decrypt entry point with a bounded DEK cache and AAD convention.

Field-level encryption
    * :mod:`codecs` / :mod:`normalize` — value⇄bytes codecs and canonicalisers.
    * :mod:`field` — the declarative :class:`~field.FieldSpec` + the
      :class:`~field.FieldEncryptor` runtime.
    * :mod:`types` — SQLAlchemy :class:`~types.EncryptedType` so models adopt
      encryption transparently; :mod:`registry` wires the active provider.

Searchable encryption
    * :mod:`deterministic` — equality-searchable (AES-GCM-SIV, derived SIV).
    * :mod:`blind_index` — keyed, irreversible equality/prefix/range tokens.

Tokenization
    * :mod:`tokenization` — a format-preserving PII vault with detokenize-under-authz.

Operations
    * :mod:`rotation` — online, batched KEK re-wrap and DEK re-encryption.
    * :mod:`models` — the durable ``crypto_*`` tables (registry, vault, audit,
      blind index, rotation jobs).

See ``app/zerotrust/crypto/DESIGN.md`` for the architecture and threat model.
"""

from __future__ import annotations

from app.zerotrust.crypto.aead import Algorithm, Envelope, generate_key, open_, seal
from app.zerotrust.crypto.context import AssociatedData, Ciphertext, CryptoProvider
from app.zerotrust.crypto.errors import (
    AuthorizationError,
    CryptoConfigError,
    CryptoError,
    DecryptionError,
    EncryptionError,
    KeyNotFoundError,
    KeyStateError,
    TokenizationError,
)
from app.zerotrust.crypto.field import FieldEncryptor, FieldSpec, SearchArtifacts
from app.zerotrust.crypto.keys import (
    DataKey,
    KeyManagementService,
    KeyState,
    WrappedDek,
)
from app.zerotrust.crypto.kms import SoftwareKMS, from_env_root_key

__all__ = [
    "Algorithm",
    "AssociatedData",
    "AuthorizationError",
    "Ciphertext",
    "CryptoConfigError",
    "CryptoError",
    "CryptoProvider",
    "DataKey",
    "DecryptionError",
    "EncryptionError",
    "Envelope",
    "FieldEncryptor",
    "FieldSpec",
    "KeyManagementService",
    "KeyNotFoundError",
    "KeyState",
    "KeyStateError",
    "SearchArtifacts",
    "SoftwareKMS",
    "TokenizationError",
    "WrappedDek",
    "from_env_root_key",
    "generate_key",
    "open_",
    "seal",
]
