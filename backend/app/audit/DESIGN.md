# `app.audit` — tamper-evident audit log + provenance

A structured, hash-chained, redaction-aware account of every **consequential**
action across Kinora — so a clip, a canon fact, or a session can be explained
end-to-end for debugging, compliance, and provenance. Additive and
self-contained under `app/audit/`; nothing in the existing schema is modified.

## Why a dedicated audit log (vs. structured logs / the compliance ledger)

* **Structured logs** (`app.core.logging`) are ephemeral and unordered — great for
  ops, useless as an evidentiary trail.
* **The compliance ledger** (`app.compliance.ledger`) is the *DPO/regulator*
  surface, keyed to data subjects and compliance categories. This subsystem is
  the *engineering/provenance* surface: it records the six agents mutating canon,
  the Showrunner's arbitration, the §9.7 render accept/degrade, budget spend, and
  flag/config flips — keyed to the **artifact** (clip / canon fact / shot) so you
  can reconstruct *how a clip came to look the way it does*.

The two share the same hash-chain idea on purpose; they do not share storage.

## What is recorded — the taxonomy (`taxonomy.py`)

A closed vocabulary so the query layer filters on stable values and the hash
commits to a stable projection:

* **actor kind** — `agent` | `user` | `system`;
* **category** — `canon` · `arbitration` · `render` · `budget` · `auth` ·
  `config` · `flag` · `scheduler` · `ingest` · `moderation` · `system`;
* **action** — a past-tense verb (`canon.updated`, `arbitration.resolved`,
  `render.degraded`, `budget.spent`, `auth.locked_out`, `flag.enabled`, …), with
  `other` as the explicit escape hatch so a new call site never blocks;
* **severity** — `info` | `notice` | `warning` | `critical` (auth/config skew
  higher by default).

`AuditEvent` (`events.py`, pydantic v2) is the typed input: who / what / on-what
(`target_type`+`target_id`) / before·after / why (`reason`) /
correlation·trace id / `occurred_at`. A coherence check rejects an incoherent
`(category, action)` early.

## Tamper-evidence — two layers (`chain.py`)

1. **Per-entry hash chain.** `entry_hash = sha256(prev_hash ||
   canonical_json(record_core))`. The core covers *every* logical field
   (including before/after), so any edit changes the hash; the chained `prev_hash`
   + contiguous `seq` mean a delete leaves a gap and an insert mismatches the
   predecessor. `recompute_chain` re-derives the whole chain and pinpoints the
   first divergence.
2. **Merkle checkpoints.** Every `audit_segment_size` entries the log seals a
   checkpoint: a Merkle root over the segment's entry hashes (duplicate-last-leaf
   construction, domain-separated leaf/node tags). A checkpoint is a compact,
   publishable commitment a third party can countersign, and it survives even a
   full-table rewrite. `merkle_proof` / `verify_merkle_proof` give an O(log n)
   inclusion proof for any single entry. Checkpoints are themselves chained
   (`checkpoint_hash`).

`AuditService.verify_integrity()` re-hashes the chain **and** re-verifies every
checkpoint root, detecting insertion / edit / deletion at either layer.

## Redaction that preserves the chain (`redaction.py`)

The conflict: tamper-evidence wants to hash the content; the right to erasure
wants to delete PII. Resolution: **commit to a hash of the value, never store the
plaintext.** At append time every sensitive field (email, password, token, ip,
name, …) becomes `{"__redacted__": <reason>, "digest": sha256(salt ||
canonical_json(value))}`. The chain hashes the *redacted* core, so:

* plaintext PII never reaches storage or the hash input;
* `verify_integrity` always passes — the hash committed to the redacted form;
* `forget_subject` is usually a no-op on payloads (PII was never stored) and is
  **idempotent** (re-redacting an already-redacted node returns it verbatim, so
  the bytes — and the hash — never change);
* an auditor holding the original value can still *prove* a match via
  `Redactor.verify`.

Non-PII (the before/after canon snapshots) stays legible — that is the point of a
provenance trail.

## Storage — pluggable sink (`store.py`, `db.py`, `db_models.py`)

`AuditSink` is the async append-and-query contract. Two implementations:

* `InMemoryAuditSink` — complete, deterministic, infra-free (tests + reference);
* `DbAuditSink` over an `AsyncSession` → two append-only tables
  (`audit_log_entries`, `audit_checkpoints`; migration `audit_0001`). The
  application assigns `seq` (unique) so the chain is deterministic; the unique
  `(seq)` constraint serialises concurrent appenders — the loser's flush raises
  `IntegrityError`, the service rolls back and retries against the new tail.
  `actor_id` / `target_id` are opaque strings with **no FK** so the proof trail
  survives deletion of whatever they reference.

## Service surface (`service.py`)

`record` (redact → hash → chain → append → maybe-seal, with seq-race retry) ·
`verify_integrity` · `query` / `count` (declarative `AuditQuery`) ·
`provenance_trail` (every event for a target, correlation-expanded, in chain
order — the full story behind a clip) · `accountability_slice` (one actor's
events) · `forget_subject` (in-place erasure, chain preserved) · `seal_segment` /
`apply_retention` (checkpoint old segments, prune sealed-and-expired entries) ·
`export` (a portable, self-verifying JSON document).

## Settings (`app.core.config`, all additive)

`audit_enabled`, `audit_segment_size` (Merkle seal cadence), `audit_retention_days`
(0 = keep forever), `audit_redaction_salt` (falls back to the JWT secret via
`audit_redaction_salt_effective`).

## Tests

* `tests/test_audit_unit.py` — deterministic, no infra: taxonomy, chain
  determinism + Merkle proofs, redaction (commit/verify/idempotence), the event
  model, and the full service — **tamper detection for insert/edit/delete**,
  query filters + pagination, **provenance trail reconstruction for a clip**,
  **redaction preserves the chain**, auto-seal + checkpoint verification,
  retention/sealing, export, and the seq-race retry.
* `tests/test_audit_db.py` — gated on `KINORA_TEST_DATABASE_URL`: the same
  service against the real Postgres sink (record/verify/query/provenance + the
  unique-`seq` serialisation guarantee).
