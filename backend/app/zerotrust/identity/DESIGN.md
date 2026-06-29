# Zero-Trust — Workload Identity + Key Management (facet A) — DESIGN.md

Owner domain (Agent: zero-trust, facet **A**). NEW, self-contained package
`backend/app/zerotrust/identity/`. This is the **service-to-service** trust
fabric — the machine-identity counterpart of the end-user `app.auth` plane. The
two are deliberately distinct and **do not touch each other**: `app.auth`
authenticates *humans* (passwords, MFA, sessions, RBAC); `app.zerotrust`
authenticates *workloads* (SPIFFE identities, mTLS/SVID, KMS, dynamic secrets,
who-may-call-whom policy). We **compose, never edit** the auth package.

The whole package is **pure / in-process and deterministic**: crypto is stdlib +
`cryptography` only, the clock is injectable, and **no module opens a socket on
import** — importing `app.zerotrust.identity` is side-effect free, so it never
perturbs the lazy composition root or the hermetic unit suite.

## Scope delivered (facet A)
1. **Workload identity** — SPIFFE IDs + an issuance authority that turns workload
   *attestation* + *registration* into short-lived X.509- and JWT-SVIDs.
2. **mTLS** — a root/intermediate CA chain, the SVID value types, and a pure
   handshake/verification seam that proves a peer's identity against a trust
   bundle (validity window on every cert in the path, revocation, authz hook).
3. **Short-lived issuance + rotation** — 1h default leaf TTL; a renew-before-
   expiry policy + a workload-side auto-rotating identity source.
4. **KMS + envelope encryption** — a KMS abstraction with a DEK/KEK hierarchy,
   AES-256-GCM wrap with key-id+version bound as AAD, key **versioning**,
   **rotation**, and **re-wrap** (rotate KEK without exposing plaintext); plus
   Vault-shaped secret storage (KV-v2 versioning, sealed at rest) and dynamic
   secrets with lease / renew / revoke / sweep.
5. **Policy seam** — a default-deny, deny-overrides "which workload may call
   which" engine over SPIFFE IDs (exact / path-prefix / domain / any matchers,
   action scoping, condition predicates, explainable decisions).

## Module map (`backend/app/zerotrust/identity/`)
| Module | Responsibility |
|---|---|
| `clock.py` | Injectable `Clock` seam (`SystemClock` / `FixedClock` / `ManualClock`) — every TTL/rotation/expiry computes against it. |
| `errors.py` | Typed error hierarchy rooted at `ZeroTrustError`. |
| `spiffe.py` | `SpiffeId` / `TrustDomain` value types — parse/validate, segment-safe `is_under` (no prefix-confusion). |
| `keys.py` | `SigningKey` / `PublicKey` over EC-P256 (ES256) + Ed25519 (EdDSA). |
| `svid.py` | `X509Svid` / `JwtSvid` carriers; `spiffe_id_of_cert` (single URI SAN). |
| `ca.py` | `CertificateAuthority` (root + intermediate, short-lived leaf issuance, revocation) + `TrustBundle`. |
| `mtls.py` | `SvidVerifier` (chain build → validity → revocation → authz) + `simulate_handshake` (mutual). |
| `jwt_svid.py` | `JwtSvidMinter` / `JwtSvidVerifier` / `JwtKeyRegistry` — minimal JOSE (ES256 raw R‖S), strict (no alg-confusion / `none`). |
| `attestation.py` | `Selector`, `AttestationResult`, `WorkloadAttestor` seam, `StaticAttestor` (test), subset-match rule. |
| `registry.py` | `WorkloadRegistry` / `RegistrationEntry` — selector→SPIFFE-ID bindings, most-specific match wins. |
| `issuer.py` | `IdentityIssuer` — attestation + registry + CA → X.509/JWT SVIDs; exposes trust material. |
| `rotation.py` | `RotationPolicy` + `WorkloadIdentitySource` (auto-rotating handle, rotation history). |
| `kms.py` | `KeyManagementService` Protocol + `LocalKms` (DEK/KEK, versioning, rotation, re-wrap) + `EnvelopeCipher`. |
| `secrets.py` | `SecretStore` (sealed KV-v2) + `DynamicSecretEngine` (`DynamicSecretRole` / `Lease` / renew / revoke / sweep). |
| `policy.py` | `AuthorizationPolicy` + matchers + `PolicyRule` + `Decision`. |
| `bundle.py` | SVID/chain + trust-bundle (de)serialization (PEM + JSON) and `federate` for multi-domain meshes — the wire codec for crossing process boundaries. |
| `contracts.py` | The `Protocol`s sibling facets consume (see below). |
| `fabric.py` | `IdentityFabric` — the pre-wired facade a composition root would build. |
| `__init__.py` | Curated public surface (~90 exports). |

## Contracts for sibling facets (`contracts.py`)
Sibling zero-trust facets (mesh enforcement, audit, secret consumers) depend
**only** on these structural `Protocol`s, never on the concretes:
- `IdentityProvider` — mint SVIDs + expose the trust bundle (← `IdentityIssuer`).
- `PeerVerifier` — verify a presented X.509-SVID (← `SvidVerifier`).
- `TokenVerifier` — verify a JWT-SVID (← `JwtSvidVerifier`).
- `KeyManagementService` — wrap/unwrap, generate DEK, rotate, re-wrap (← `LocalKms`).
- `SecretProvider` — read static secrets (← `SecretStore`).
- `AuthorizationGate` — decide caller→target (← `AuthorizationPolicy`).

All six are satisfied **structurally**; a test asserts conformance via `isinstance`
against the `@runtime_checkable` protocols.

## Crypto / determinism stance
- Keys: stdlib + `cryptography` only. Tests load **fixed PEM** keys + a fixed
  AES KEK + a `ManualClock` at a pinned epoch, so cert windows, serials, KEK
  material, and rotation timing are exactly controllable.
- **Signatures**: ECDSA is randomised (no forced deterministic-k), so tests
  assert *verification* and *round-trip*, never signature byte-equality. Ed25519
  is deterministic. AES-GCM nonces are random → tests assert round-trip + tamper
  rejection, not ciphertext equality.
- **Rotation correctness is proven**, not asserted: old KEK versions keep
  decrypting after rotation; `rewrap` moves a DEK to the current version without
  exposing plaintext; the workload identity source rotates *exactly once* per
  window across a `ManualClock` walk; expired/revoked SVIDs are rejected.

## Additive shared-file changes
**None.** This facet is fully self-contained under `app/zerotrust/identity/`.
It does **not** modify `core/config.py`, `composition.py`, `db/models/`,
`api/routes/`, or any Alembic migration. Wiring into the composition root (a lazy
`IdentityFabric` seam) is left as an explicit, additive follow-up so the facet
ships without touching files other agents are editing in parallel.

## Tests (`backend/tests/zerotrust/`, 136 tests, all green)
`test_spiffe` · `test_ca_svid` · `test_mtls` · `test_jwt_svid` ·
`test_attestation_registry` · `test_issuer_rotation` · `test_kms` ·
`test_secrets` · `test_policy` · `test_bundle` · `test_fabric`
(+ `conftest` fixed-key fixtures).
`make lint` (ruff + mypy) is clean for this package and its tests; `make test`
runs them with no infra (hermetic).

## Remaining roadmap (future facets / follow-ups, not in scope here)
- **Composition wiring**: an additive lazy `identity_fabric` seam on the
  `Container` + additive `app/core/config.py` fields (trust-domain name, KEK id,
  leaf/JWT TTLs) — all safe defaults, deferred to avoid shared-file contention.
- **Federation**: cross-trust-domain bundle exchange + `spiffe://` JWKS endpoints.
- **Persistence**: a DB-backed `WorkloadRegistry` + KMS key-store (today in-memory)
  and an external-KMS backend (AWS/GCP/Aliyun KMS) behind `KeyManagementService`.
- **Real attestors**: Kubernetes / AWS-IID / Unix-PID `WorkloadAttestor`s behind
  the existing seam (only `StaticAttestor` ships today).
- **Sibling facets** (separate agents): a mesh/transport enforcement layer that
  consumes `PeerVerifier` + `AuthorizationGate`; an audit stream over issuance +
  rotation + authz decisions; CRL/OCSP-style revocation distribution.
