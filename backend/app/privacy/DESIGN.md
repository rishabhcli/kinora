# `app.privacy` — data retention, GDPR/CCPA right-to-erasure & DSAR enforcement

This package is the **execution / enforcement** half of Kinora's data-subject
rights story. Its sibling `app.compliance` is the **governance** half: it decides
*whether* and *for how long* data may be kept and records the lawful basis. This
package knows *which fields in which stores* physically hold a subject's data, and
it *assembles* (export) and *removes* (erasure) that data coherently across every
store. The two reconcile but neither imports the other — they are siblings that may
land independently. `app.dataportability` (the DB-coupled book/account archive
codec) is a third sibling; this package is store-agnostic and drives narrow
protocol seams instead, so it is fully unit-testable with in-memory fakes.

## Modules

| Module | Responsibility |
|---|---|
| `enums.py` | `StoreKind`, `PIICategory`, `ErasureStrategy`, `RetentionAction`, `ConsentStatus`, `ErasureState`, `StepStatus`. |
| `errors.py` | `PrivacyError` hierarchy (`DataMapError`, `LegalHoldError`, `ErasureIncompleteError`, `ChainIntegrityError`, `StoreError`). |
| `clock.py` | Injectable UTC clock (`FixedClock` for deterministic deadline tests). Local copy — no `app.compliance` dependency. |
| `hashchain.py` | The pure `sha256(prev || canonical_json(core))` chain primitive used to verify crypto-erasure integrity. |
| `datamap.py` | The **declarative PII inventory / data-map** — the Art. 30 record of processing. Drives export + erasure + the residual scan. Validates that append-only stores never declare a destructive strategy and credentials are never exportable. |
| `protocols.py` | The four store seams: `SubjectDataStore`, `BlobStore`, `EventStore`, and the **local `AuditRedactor`** protocol the hash-chained audit log will later satisfy. |
| `consent.py` | Purpose-scoped, append-only consent tracking; current status derived by folding the log (proof trail, Art. 7(1)). |
| `retention.py` | The retention-policy engine: per-data-class TTL, consent-withdrawal shortening, and **legal-hold** exceptions that block deletion. |
| `export.py` | The **DSAR export** assembler (Art. 15 / Art. 20): walks the data-map, queries every store, builds a portable, digest-stamped bundle with a coverage proof. Credentials excluded; append-only stores summarised. |
| `erasure.py` | The **right-to-erasure orchestrator** (Art. 17): per-store strategy dispatch, hold-aware, idempotent + resumable (`ErasureRun` is the resume token), and verifiable (mints a certificate). |
| `certificate.py` | The **residual scan** + verifiable **completion certificate** (hash-stamped; `verify()` re-hashes; `complete` iff zero residual AND chain intact). |
| `service.py` | `PrivacyService` facade wiring it all; mirrors the `app.compliance` `Fulfiller` shape. |

## The crypto-erasure invariant

Mutable stores (relational rows, object blobs) **hard-delete** or
**anonymize-in-place**. The append-only stores cannot — deleting an entry breaks
every subsequent integrity hash. So:

* **event store** → **crypto-erase**: destroy the subject's per-record encryption
  key. The ciphertext stays (offsets / projection hashes are untouched) but is
  permanently unreadable.
* **audit / compliance log** → **redact through `AuditRedactor`**: replace the
  subject's personal fields with a redaction marker, re-derive the affected
  entries' hashes, and re-chain the tail so `verify_chain()` still returns `True`.
  The *fact* an audited event happened survives for accountability; only the
  personal payload is gone.

The data-map enforces this at construction (`DataMapError` if an append-only field
declares `HARD_DELETE`/`ANONYMIZE`), and the orchestrator verifies the chain after
every redaction (`ChainIntegrityError` otherwise).

## Idempotent / resumable erasure

`plan_run` builds one `ErasureStep` per `(store, resource, strategy)`. Each store
seam is itself idempotent (a second delete affects 0 rows). A run that crashes is
resumed by passing the persisted `ErasureRun` back into `erase(...)`; only
`pending` steps replay. A class-scoped legal hold marks its steps `skipped`; a
subject-wide hold raises `LegalHoldError` before any destructive step.

## Verifiable completion certificate

After erasure the `ResidualScanner` re-walks the data-map and counts whatever still
maps to the subject, and verifies the append-only chain. A clean scan mints an
`ErasureCertificate` whose `certificate_hash` covers its content; `complete` is
`True` only when every store reports zero residual **and** the chain is intact.

## Settings (additive)

`privacy_retention_*_days`, `privacy_redaction_marker`, `privacy_erasure_step_batch`
in `app.core.config.Settings`. Nothing is required; defaults are the platform DPO
defaults. No `KINORA_LIVE_VIDEO` interaction; no infra/network at import or in tests.
