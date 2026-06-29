# Zero-trust data-at-rest crypto (facet B)

Application-layer encryption for Kinora. The database, object store, and backups
hold only ciphertext, tokens, and keyed search indexes; plaintext exists only
transiently in the application process, decrypted on demand under keys that are
wrapped by a KMS the application never sees the root of. (kinora.md §8 memory /
audit, §11 accountability.)

This document is the architecture + threat model. The code lives entirely under
`backend/app/zerotrust/crypto/`. **Additive only**: the sole shared-file change
is registering the five `crypto_*` tables on `Base.metadata` (see *Shared-file
changes* below).

## Layer map (bottom-up)

| Module | Responsibility |
|---|---|
| `errors.py` | Exception hierarchy; `DecryptionError` is opaque (no oracle). |
| `aead.py` | AES-256-GCM / ChaCha20-Poly1305 / AES-GCM-SIV; versioned self-describing `Envelope` wire format; AAD binding. |
| `keys.py` | **KMS contract** (`KeyManagementService` Protocol, owned by facet A) + envelope hierarchy (root → KEK → DEK), `WrappedDek`, key-state machine. |
| `kms.py` | `SoftwareKMS`: HKDF-over-root-key implementation of the contract; deterministic under a fixed root (for tests + self-hosting). |
| `context.py` | `CryptoProvider`: high-level encrypt/decrypt, bounded LRU DEK cache, `AssociatedData`, column-search-seed derivation. |
| `codecs.py` / `normalize.py` | value⇄bytes codecs; canonicalisers for searchable input. |
| `field.py` | Declarative `FieldSpec` + `FieldEncryptor` (per-record DEK, search artefacts). |
| `deterministic.py` | Equality-searchable encryption (AES-GCM-SIV + derived SIV). |
| `blind_index.py` | Keyed irreversible equality / prefix / range tokens. |
| `tokenization.py` | Format-preserving PII vault; detokenize-under-authz; audit hook. |
| `types.py` / `registry.py` | SQLAlchemy `EncryptedType` so models adopt encryption transparently; process-wide provider binding. |
| `rotation.py` | Online, batched KEK re-wrap + DEK re-encryption with compare-and-set safety. |
| `models.py` | The durable `crypto_*` tables. |
| `repositories.py` | DB-backed `TokenStore`, blind-index repo, KEK-registry sync, rotation-job ledger. |

## Envelope key hierarchy

```
root master key   (HSM / cloud KMS; never crosses the seam)
   │ wraps
KEK   one logical key per data domain ("pii", "books"); versioned; rotatable
   │ wraps
DEK   one per record; the only key a ciphertext is actually sealed under
```

- **Per-record DEKs** → rotating/destroying one DEK affects exactly one record
  (crypto-shredding for GDPR erasure).
- **KEK rotation is cheap**: re-*wrap* DEKs (small), never touch bulk ciphertext.
- **`SoftwareKMS` derives** KEKs from the root via HKDF, so a fixed root makes the
  whole hierarchy deterministic — the crypto-correctness + rotation tests assert
  exact round-trips under known keys.

## Searchable encryption

Two complementary mechanisms, both keyed off a **column-stable** search seed
(`CryptoProvider.column_search_seed` → KMS `derive_purpose_key`, bound to the KEK
version so a KEK rotation rolls the search keys too):

- **Deterministic encryption** (`deterministic.py`) — AES-GCM-SIV with a SIV
  derived (HMAC) from the normalised plaintext. Equal plaintext → equal
  ciphertext → `WHERE col_det = :probe`. Misuse-resistant, so the deterministic
  nonce is safe (plain GCM here would leak the auth key).
- **Blind indexes** (`blind_index.py`) — truncated keyed HMAC tokens stored in the
  `crypto_blind_index` companion table: equality, per-prefix (`LIKE 'x%'`), and
  coarse range buckets (`floor(v/bucket)`, with boundary decrypt-and-filter for
  exactness). Irreversible and keyed: useless to a table thief.

Privacy cost is explicit and documented: deterministic/equality reveals *which
rows share a value*; range buckets reveal coarse ordering. Use randomised
`EncryptedType` for anything that does not need search.

## Tokenization vault

`tokenization.py` mints format-preserving surrogates (length + alphabet
preserved, optional literal prefix/suffix) with **no algebraic relationship** to
the plaintext. The real value is AEAD-encrypted at rest in `crypto_token_vault`,
keyed by the token (bound as AAD). `detokenize` requires an authorised purpose
per the token's `TokenPolicy`; every attempt (allow + deny) is appended to
`crypto_token_access_log`. Deterministic tokenization dedups repeats to one row;
random tokenization maximises unlinkability.

## Rotation (online, batched, resumable)

`rotation.py` runs concurrently with live traffic:
- **REWRAP** (cheap): new KEK version, re-wrap DEKs; old version destroyed once
  drained. `crypto_rotation_job` is the durable cursor + counters.
- **REENCRYPT** (full): decrypt + re-encrypt under fresh DEKs (suspected exposure
  / algorithm migration).
Both use a caller-supplied **compare-and-set** write-back: a concurrent writer
wins, the rotator skips and retries next pass — idempotent, no lost updates, no
long locks. The KMS opens a decrypt-only drain window (`PENDING_DELETION`) on the
old KEK version exactly for this.

## Threat model (what this defends)

| Threat | Defence |
|---|---|
| Stolen DB dump / backup | Only ciphertext + keyed tokens; KEK/root in the KMS. |
| Stolen DB **and** search index | Tokens are keyed HMAC; useless without the seed. |
| Ciphertext relocated to another row/column (confused deputy) | AAD binds `(table, column, record_id)`; relocation fails the AEAD tag. |
| Tampered ciphertext / forged tag | AEAD authentication; `DecryptionError`. |
| Nonce reuse on searchable columns | AES-GCM-SIV (misuse-resistant), never plain GCM. |
| Padding / validity oracle | `DecryptionError` is opaque; all integrity failures collapse to one message. |
| Insider reading PII via the app | Detokenize requires an authorised purpose + is audited. |
| Key compromise / "re-key everything" mandate | KEK + DEK rotation jobs; per-record DEKs enable crypto-shredding. |

**Out of scope here:** transport encryption (TLS), disk/volume encryption,
HSM/root-key provisioning (facet A), and HTTP-layer authn/z (the auth plane). The
vault's purpose check is a *defence in depth* at the crypto layer, not a
replacement for request auth.

## KMS seam (facet A)

`keys.KeyManagementService` is the narrow Protocol this facet consumes:
`generate_data_key`, `unwrap_data_key`, `rewrap_data_key`, `derive_purpose_key`.
Facet A owns the production implementation (HSM / cloud KMS). Until it lands,
`kms.SoftwareKMS` satisfies the contract so the facet is self-sufficient and
testable. Swapping in facet A's KMS is a one-line wiring change at the
composition root; nothing above the seam changes.

## Shared-file changes (additive)

- `backend/app/db/models/__init__.py` — import the five `crypto_*` models so
  Alembic autogenerate + `create_all` register them on `Base.metadata`, plus
  their names in `__all__`. No existing entry modified.
- `backend/migrations/versions/tokvault_0001_*.py` — new migration `tokvault_0001`
  (chains off the auth security plane head `f7a2b9c4d1e8`), creating the five
  tables. Touches no existing table; full `downgrade`.
- `backend/app/zerotrust/__init__.py` — new package docstring (the `zerotrust`
  namespace was an empty dir shared with sibling facets).
