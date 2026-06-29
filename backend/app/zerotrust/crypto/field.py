"""The declarative field-level-encryption framework.

A :class:`FieldSpec` declares, per column, *how* a value is protected — which
codec serialises it, whether it is searchable, and under which KEK — once, near
the model. The :class:`FieldEncryptor` is the runtime that applies a spec: it
wires the spec's choices to the :class:`~app.zerotrust.crypto.context.CryptoProvider`
and produces/consumes the on-row payload.

Storage payload
---------------
An encrypted field serialises to one self-contained, base64-free blob via
:class:`StoredField` so it fits a single ``BYTEA``/``BLOB`` column:

``payload = len(wrapped_dek) varint | wrapped_dek_json | envelope``

The wrapped DEK is JSON because it is small structured metadata
(``kek_id``/``version``/``algorithm``) and must survive a KEK rotation that
rewrites only this prefix; the envelope is the bulk ciphertext. Decoding never
needs external metadata, which is what lets the SQLAlchemy type be a drop-in.

Searchability
-------------
A spec may additionally request a *deterministic* ciphertext (for equality) and
*blind-index* tokens (equality / prefix / range). Those derived artefacts are
computed here from per-record key material and surfaced to the type decorator,
which writes them to companion columns. The framework keeps the relationship
between the encryption key and the search keys inside one hierarchy
(:meth:`CryptoProvider.deterministic_key` / ``derive_blind_index_key``), so a
field is never searchable under a key unrelated to the one that encrypts it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.zerotrust.crypto import blind_index as bidx
from app.zerotrust.crypto import deterministic as det
from app.zerotrust.crypto import normalize
from app.zerotrust.crypto.aead import Algorithm
from app.zerotrust.crypto.codecs import STRING, Codec
from app.zerotrust.crypto.context import AssociatedData, Ciphertext, CryptoProvider
from app.zerotrust.crypto.errors import CryptoConfigError, DecryptionError
from app.zerotrust.crypto.keys import WrappedDek


def _write_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


@dataclass(frozen=True, slots=True)
class SearchArtifacts:
    """The optional searchable side-outputs computed for one field value.

    Attributes:
        deterministic: a deterministic ciphertext for an equality column, or None.
        equality_index: a blind equality token, or None.
        prefix_indexes: blind prefix tokens (for ``LIKE 'x%'``), possibly empty.
        range_bucket: a coarse blind range-bucket token, or None.
    """

    deterministic: bytes | None = None
    equality_index: bytes | None = None
    prefix_indexes: tuple[bytes, ...] = ()
    range_bucket: bytes | None = None


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """Declarative protection policy for a single encrypted column.

    Attributes:
        codec: how the Python value serialises to bytes (default UTF-8 string).
        kek_id: the KEK under which this field's per-record DEKs are wrapped.
        algorithm: the randomised AEAD for the primary ciphertext.
        searchable_equality: also emit a deterministic ciphertext for ``=`` search.
        blind_equality: also emit a blind equality token.
        blind_prefix: emit blind prefix tokens up to ``prefix_max_len``.
        blind_range: emit a blind range-bucket token (numeric fields).
        normalizer: registry name of the canonicaliser applied before any search
            transform (so equality is case/whitespace-insensitive as configured).
        prefix_max_len: cap on prefix-token generation.
        range_bucket_size: bucket width for blind range buckets.
    """

    codec: Codec[Any] = STRING
    kek_id: str | None = None
    algorithm: Algorithm = Algorithm.AES_256_GCM
    searchable_equality: bool = False
    blind_equality: bool = False
    blind_prefix: bool = False
    blind_range: bool = False
    normalizer: str = "casefold"
    prefix_max_len: int = 16
    range_bucket_size: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.blind_range and self.range_bucket_size <= 0:
            raise CryptoConfigError("range_bucket_size must be positive for blind_range")
        # Validate the normaliser name eagerly so a typo fails at wiring time.
        normalize.resolve(self.normalizer)

    @property
    def wants_search(self) -> bool:
        """Whether any searchable artefact is requested."""
        return (
            self.searchable_equality
            or self.blind_equality
            or self.blind_prefix
            or self.blind_range
        )


@dataclass(frozen=True, slots=True)
class StoredField:
    """The on-row representation of an encrypted value (ciphertext + wrapped DEK)."""

    ciphertext: Ciphertext

    def to_bytes(self) -> bytes:
        """Serialise to the single ``varint(len)|wrapped_dek_json|envelope`` blob."""
        import json

        w = self.ciphertext.wrapped_dek
        meta = json.dumps(
            {
                "kek_id": w.kek_id,
                "kek_version": w.kek_version,
                "algorithm": int(w.algorithm),
                "wrapped": w.ciphertext.hex(),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        return _write_varint(len(meta)) + meta + self.ciphertext.envelope

    @classmethod
    def from_bytes(cls, blob: bytes) -> StoredField:
        import json

        try:
            meta_len, pos = _read_varint(blob, 0)
            meta = json.loads(blob[pos : pos + meta_len].decode("utf-8"))
            envelope = blob[pos + meta_len :]
            wrapped = WrappedDek(
                kek_id=meta["kek_id"],
                kek_version=int(meta["kek_version"]),
                ciphertext=bytes.fromhex(meta["wrapped"]),
                algorithm=Algorithm(int(meta["algorithm"])),
            )
        except (KeyError, ValueError, IndexError, UnicodeDecodeError) as exc:
            raise DecryptionError("corrupt encrypted-field payload") from exc
        return cls(ciphertext=Ciphertext(envelope=envelope, wrapped_dek=wrapped))


class FieldEncryptor:
    """Applies a :class:`FieldSpec` using a :class:`CryptoProvider`."""

    def __init__(self, provider: CryptoProvider) -> None:
        self._provider = provider

    def encrypt(
        self, spec: FieldSpec, value: Any, aad: AssociatedData
    ) -> tuple[bytes, SearchArtifacts]:
        """Encrypt ``value`` per ``spec``; return the stored blob + search artefacts."""
        plaintext = spec.codec.encode(value)
        ct = self._provider.encrypt(
            plaintext,
            aad,
            algorithm=spec.algorithm,
            kek_id=spec.kek_id or self._provider.kek_id,
        )
        stored = StoredField(ciphertext=ct).to_bytes()
        artifacts = (
            self._derive_search(spec, ct, value, aad)
            if spec.wants_search
            else SearchArtifacts()
        )
        return stored, artifacts

    def decrypt(self, spec: FieldSpec, blob: bytes, aad: AssociatedData) -> Any:
        """Decrypt a stored blob back to the typed value (round-trips the codec)."""
        stored = StoredField.from_bytes(blob)
        plaintext = self._provider.decrypt(stored.ciphertext, aad)
        return spec.codec.decode(plaintext)

    def search_tokens(self, spec: FieldSpec, value: Any) -> SearchArtifacts:
        """Compute the search artefacts for a *query probe* (no record DEK exists).

        Equality search needs query-time tokens that match what was stored. The
        deterministic + blind-index keys are derived from a **column-stable**
        secret — the KMS DEK for the column's dedicated search key — rather than a
        per-record DEK, otherwise two records would never produce matching tokens.
        See :meth:`_search_keys`.
        """
        norm = normalize.resolve(spec.normalizer)
        normalised = norm(self._as_text(spec, value))
        det_key, bidx_key, siv_key = self._search_keys(spec)
        return self._artifacts_from_normalised(spec, normalised, value, det_key, bidx_key, siv_key)

    # -- internals ----------------------------------------------------------- #

    def _derive_search(
        self, spec: FieldSpec, ct: Ciphertext, value: Any, aad: AssociatedData
    ) -> SearchArtifacts:
        norm = normalize.resolve(spec.normalizer)
        normalised = norm(self._as_text(spec, value))
        det_key, bidx_key, siv_key = self._search_keys(spec)
        return self._artifacts_from_normalised(spec, normalised, value, det_key, bidx_key, siv_key)

    def _artifacts_from_normalised(
        self,
        spec: FieldSpec,
        normalised: bytes,
        value: Any,
        det_key: bytes,
        bidx_key: bytes,
        siv_key: bytes,
    ) -> SearchArtifacts:
        deterministic = (
            det.encrypt_deterministic(det_key, siv_key, normalised)
            if spec.searchable_equality
            else None
        )
        equality = bidx.equality_index(bidx_key, normalised) if spec.blind_equality else None
        prefixes = (
            tuple(bidx.prefix_indexes(bidx_key, normalised, max_len=spec.prefix_max_len))
            if spec.blind_prefix
            else ()
        )
        bucket = None
        if spec.blind_range:
            bucket = bidx.range_buckets(
                bidx_key, int(value), bucket_size=spec.range_bucket_size
            )
        return SearchArtifacts(
            deterministic=deterministic,
            equality_index=equality,
            prefix_indexes=prefixes,
            range_bucket=bucket,
        )

    def _search_keys(self, spec: FieldSpec) -> tuple[bytes, bytes, bytes]:
        """Return ``(det_key, blind_index_key, siv_key)`` — column-stable secrets.

        Derived from a deterministic KMS unwrap of a *column search key*. We use a
        fixed wrapped-DEK seed per (kek_id) by generating one stable DEK via the
        KMS the first time and caching it on the provider; for the software KMS
        this is derived from the root key so it is stable across processes. The
        three keys are domain-separated from one another.
        """
        seed = self._column_seed(spec)
        det_key = self._provider.deterministic_key(seed)
        bidx_key = self._provider.derive_blind_index_key(seed)
        # SIV key is domain-separated from the deterministic enc key.
        siv_key = self._provider.derive_blind_index_key(det_key)
        return det_key, bidx_key, siv_key

    def _column_seed(self, spec: FieldSpec) -> bytes:
        """A column-stable 32-byte secret for search-key derivation.

        Implemented via the provider's KMS: deterministically derive from the
        active KEK so the seed is identical for every record in the column and
        survives restarts (the software KMS derives KEKs from a fixed root). This
        is *not* a per-record DEK — search keys must be column-wide to match.
        """
        return self._provider.column_search_seed(spec.kek_id or self._provider.kek_id)

    @staticmethod
    def _as_text(spec: FieldSpec, value: Any) -> str:
        """Coerce ``value`` to text for normalisation (ints/bytes included)."""
        if isinstance(value, str):
            return value
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="surrogatepass")
        return str(value)


__all__ = [
    "FieldEncryptor",
    "FieldSpec",
    "SearchArtifacts",
    "StoredField",
]
