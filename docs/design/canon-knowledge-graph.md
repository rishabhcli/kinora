# DESIGN — Bitemporal Knowledge-Graph Canon Engine

Domain owner: **Memory / MCP canon server** — `backend/app/memory/` + `backend/app/mcp/`.

This document is the living architecture + roadmap for turning Kinora's canon from a
**uni-temporal versioned graph** into a **bitemporal knowledge-graph engine** with
time-travel, CRDT-merged concurrent writes, canon FORK / DIFF / MERGE, an append-only
audit log, graph reasoning, and a clean inspectable read contract — while preserving the
two hard invariants of this domain:

1. `MemoryTools.dispatch` is **the single execution path** for every MCP tool.
2. §8.5 forgetting semantics: a fact is **scoped to the beat interval where it was true**
   (`valid_to_beat` close, never delete); retired facts vanish from forward retrieval but
   survive for backward / time-travel reads.

---

## 0. Vocabulary — the two time axes (and the third dimension)

The existing canon has exactly **one** time axis:

- **VALID time** = the *story* timeline, measured in **beat ordinals**
  (`valid_from_beat` .. `valid_to_beat`). "When, in the book, was this true?"

A bitemporal store adds a second, orthogonal axis:

- **TRANSACTION time** = the *database* timeline, measured in **wall-clock UTC**
  (`tx_from` .. `tx_to`). "When did the system believe this, and when did it stop?"

A fact is corrected (a director edit, a Critic conflict resolution) by **closing its
transaction interval** (`tx_to = now()`) and inserting a successor row — the old row is
never mutated in place beyond that one close, so every past *belief* is reconstructable.
This is what makes "canon as of any past write" answerable: filter `tx_from <= T < tx_to`.

We also add a third, non-temporal dimension for concurrency:

- **BRANCH** = a named line of canon. `main` is the trunk; a director edit can `FORK`
  a branch, accumulate edits, then `DIFF` and `MERGE` back. CRDT metadata on each fact
  makes concurrent writes from multiple reading sessions conflict-free.

> A point query is therefore four-dimensional: *(book, branch, valid-beat, tx-time)*.

---

## 1. Subsystems (modules in `backend/app/memory/`)

| Module | Responsibility | Status |
|---|---|---|
| `bitemporal.py` | Pure value objects: `BeatInterval`, `TxInterval`, `BitemporalCoord`, interval algebra (overlap / contains / Allen relations). No I/O. | **DONE** |
| `crdt.py` | Pure CRDT cores: `HLC` (hybrid logical clock), `LWWRegister`, `ORSet`, `GCounter`, `VersionVector`. Conflict-free `merge`. No I/O. | **DONE** |
| `audit_log.py` | Append-only canon audit service over the `canon_audit` table — every mutation is one immutable row with a hash-chain (`prev_hash` → tamper-evident). | **DONE** |
| `branch_service.py` | FORK / DIFF / MERGE over branches: create a branch off a base coord, structural diff between two branches, three-way CRDT merge back to `main`. | **DONE** |
| `temporal_state_service.py` | The bitemporal continuity-fact engine: assert / correct / retire facts carrying both intervals + branch + CRDT stamp; `as_of(beat, tx, branch)` reconstruction. Wraps `BitemporalStateRepo`. | **DONE** |
| `graph_reasoning.py` | Pure graph reasoning over a canon snapshot: relationship graph build, reachability, shortest path, contradiction detection, entity neighborhoods. No I/O. | **DONE** |
| `retrieval.py` | Scalable semantic retrieval helpers: MMR re-ranking, cosine math, hybrid lexical+vector scoring, k-budget packing under a token ceiling (§8.4). Pure. | **DONE** |
| `canon_reasoner.py` | Query-time façade binding bitemporal reads + graph reasoning + retrieval. | **DONE** |
| `compaction.py` | Temporal GC: prune redundant superseded tx-rows beyond a retention horizon, audit-safe (keeps current + newest superseded belief; never touches the hash-chain). | **DONE** |
| `bitemporal_vault.py` | Inspectable markdown export of the 4-D canon (active facts + branches + per-fact tx-histories + audit trail). Complements the existing `canon_vault.py`, doesn't modify it. | **DONE** |
| `canon_service.py` | (existing) §8.4 retrieval policy — left intact; bitemporal reads are layered alongside. | unchanged |

| Table (Alembic) | Purpose | Status |
|---|---|---|
| `canon_audit` | Append-only, hash-chained mutation log. | **DONE** (migration `a1b2c3d4e5f6`) |
| `bitemporal_states` | Continuity facts with VALID + TX intervals, `branch`, CRDT stamp. | **DONE** (migration `a1b2c3d4e5f6`) |
| `canon_branches` | Branch registry (name, base coord, status, parent). | **DONE** (migration `a1b2c3d4e5f6`) |

All new tables are **additive**, stacked on the current head `f1c2a3b4d5e6`. They do not
touch `entities` or `continuity_states`, so the existing uni-temporal path keeps working
unchanged; the bitemporal engine is a parallel, opt-in store the new tools write.

---

## 2. Contracts (Pydantic, in `backend/app/memory/contracts.py`)

The new tool I/O and read-contract types live in `contracts.py` (mirrors how
`interfaces.py` holds the existing contracts). Highlights:

- `BitemporalFact` — a single fact row projected: subject/predicate/object, `valid`
  (BeatInterval), `tx` (TxInterval as ISO-8601), `branch`, `stamp` (CRDT), `audit_id`.
- `FactHistory` — the full transaction-time history of one logical fact.
- `BranchInfo`, `BranchDiff`, `MergeResult` — fork/diff/merge contracts.
- `AuditEntry` — one audit-log row with its hash-chain links.
- `CanonReadView` — the clean, inspectable read contract the frontend consumes.

---

## 3. MCP tool surface additions (preserve `dispatch`)

Every new tool is registered in `TOOL_DEFS` and routed through `MemoryTools.dispatch`
— **no second execution path**. New tools (all read-or-additive, none spends video):

| Tool | Signature | Purpose |
|---|---|---|
| `canon.assert_fact` | `(subject, predicate, object, valid_from, branch?) → fact` | Bitemporal assert (tx_from + audit + CRDT stamp). |
| `canon.correct_fact` | `(fact_id, new_object, …) → fact` | Correct a belief: close tx interval, insert successor. |
| `canon.retire_fact` | `(fact_id, valid_to)` | §8.5 forgetting on the bitemporal store. |
| `canon.facts_as_of` | `(book, beat, as_of_tx?, branch?) → facts[]` | 4-D time-travel read. |
| `canon.fact_history` | `(fact_id) → history` | Every past belief of one fact (tx timeline). |
| `canon.fork` | `(book, name, base_beat?, base_tx?) → branch` | Create an editing branch. |
| `canon.diff` | `(book, branch_a, branch_b) → diff` | Structural diff between branches. |
| `canon.merge` | `(book, source, target=main) → merge_result` | Three-way CRDT merge. |
| `canon.audit` | `(book, limit?) → entries[]` | Replay the append-only audit log. |
| `canon.view` | `(book, beat?, branch?) → read_view` | The inspectable read contract. |
| `canon.compact` | `(book, branch?, horizon_days?, dry_run?) → report` | Temporal GC of superseded tx-history (dry-run by default). |
| `canon.vault` | `(book, branch?, beat?, history_for?) → markdown` | Render the bitemporal canon to inspectable markdown. |

**Total tool surface: 27** (15 original + 12 bitemporal), all through `MemoryTools.dispatch`.
The MCP-protocol round-trip test asserts this exact count so the single execution path is
guarded.

---

## 4. CRDT model — why concurrent writes are conflict-free

Multiple reading sessions may edit canon at once (two directors, or a director + the
Continuity Supervisor auto-resolving a Critic conflict). We make writes commutative:

- Each fact carries a **stamp**: `(hlc, actor_id)` where `hlc` is a Hybrid Logical Clock
  (monotone, causality-respecting, wall-clock-anchored). The object value of a fact is an
  **LWW-Register** keyed by `(hlc, actor_id)` — concurrent sets resolve deterministically
  to the highest stamp, identically on every replica.
- A fact's *existence* (assert vs retire) is an **OR-Set** add/remove with unique tags, so
  "assert on branch A" and "retire on branch B" merge without a lost update.
- Branch heads track a **VersionVector** so MERGE knows which side saw which writes and can
  do a true three-way merge (only genuinely concurrent edits need a rule; the rest fast-fwd).

The CRDT cores are pure and exhaustively unit-tested for the algebraic laws
(commutativity, associativity, idempotence) so the distributed behavior is provable
offline with **no DB and no network** (matches the unit-suite-runs-with-no-infra rule).

---

## 5. Integration & cross-domain contracts

**Owned, internal:** all of `backend/app/memory/*` and `backend/app/mcp/*`.

**Shared files touched (ADDITIVE ONLY — recorded here per the worktree rules):**

- `backend/app/db/models/__init__.py` — register the 3 new ORM models on `Base.metadata`
  (append-only import + `__all__` entries). Required so Alembic/`create_all` see them.
  No existing line changed.
- `backend/migrations/versions/a1b2c3d4e5f6_*.py` — new migration, `down_revision =
  f1c2a3b4d5e6` (current head). Creates only the 3 new tables; no ALTER of existing tables.

No other domain's files are modified. The render pipeline, scheduler, agents, and API
routes are untouched; they continue to use the existing `canon_service` / `continuity`
path. The bitemporal engine is reachable only through the new MCP tools and the new
services, so adoption by sibling domains is opt-in and non-breaking.

**Seams left for later phases** (declared, not implemented here):
- Wiring `canon.merge` conflict outcomes into the §7.2 conflict log + director route.
- Backfilling `continuity_states` → `bitemporal_states` (a one-shot migration job).
- A pgvector ANN index swap for `retrieval.py` MMR at novel scale.

---

## 6. Phased roadmap

- **Phase 1 — Bitemporal foundation** ✅ DONE
  Interval algebra, `bitemporal_states` + `canon_audit` + `canon_branches` tables &
  migration, `BitemporalStateRepo`, `TemporalStateService` (assert/correct/retire/as_of),
  `AuditLog` hash-chain. Unit + DB-integration tests.
- **Phase 2 — CRDT concurrency core** ✅ DONE
  `HLC`, `LWWRegister`, `ORSet`, `GCounter`, `VersionVector`; algebraic-law tests; fact
  stamping wired into the temporal service.
- **Phase 3 — FORK / DIFF / MERGE** ✅ DONE
  `branch_service.py`: fork off a coord, structural diff, three-way CRDT merge to main,
  audit entries for branch ops. Tests for fast-forward, divergent, conflicting merges.
- **Phase 4 — Graph reasoning** ✅ DONE
  `graph_reasoning.py`: relationship graph, reachability/shortest-path, contradiction
  detection, entity neighborhoods. Pure, unit-tested.
- **Phase 5 — Scalable semantic retrieval** ✅ DONE
  `retrieval.py`: cosine/normalize, MMR re-rank, hybrid lexical+vector scoring, k-budget
  packing under a token ceiling. Pure, unit-tested.
- **Phase 6 — MCP tool surface + read contract** ✅ DONE
  `contracts.py`, 10 new tools through `dispatch`, `canon.view` inspectable read contract.
- **Phase 7 — Reasoning service integration** ✅ DONE
  `canon_reasoner.py`: bind graph reasoning + retrieval + bitemporal reads into a single
  query-time façade.
- **Phase 8 — Temporal GC + inspectable bitemporal vault** ✅ DONE
  `compaction.py` (audit-safe pruning of superseded tx-history beyond a horizon) +
  `bitemporal_vault.py` (markdown export of the 4-D canon), wired as `canon.compact` /
  `canon.vault`. Unit + DB-integration tests (including a tamper-then-detect proof and a
  "current belief survives compaction" proof).

### Test coverage delivered
- `tests/test_memory_crdt.py` — 14 pure CRDT-law tests (commutativity / associativity /
  idempotence for LWW, OR-Set, G-Counter, VersionVector; HLC monotonicity).
- `tests/test_memory_bitemporal_algebra.py` — pure interval algebra, Allen relations, graph
  reachability/shortest-path/neighborhood, contradiction detection, retrieval math (MMR
  diversity / hybrid scoring / budget packing), and bitemporal-vault rendering.
- `tests/test_bitemporal_engine.py` — DB-integration: assert/correct/retire/as-of/history,
  the "canon as of any past write" property, the hash-chained audit log + tamper detection,
  FORK/DIFF/MERGE (fast-forward + concurrent-edit LWW conflict), compaction, and every new
  tool through `MemoryTools.dispatch`. SKIPs cleanly with no `KINORA_TEST_DATABASE_URL`.

Run: pure tests need **no infra**; integration tests run against an **isolated** DB
(`kinora_conflict_test`, Postgres :5433) per the project's test-isolation rule — never the
live `kinora` DB.

### Remaining (future runs)
- Materialized branch-head VersionVector cache (avoid recomputation on every merge).
- pgvector HNSW index + ANN recall benchmark for `retrieval.py` at 300-page scale.
- Backfill job `continuity_states` → `bitemporal_states`; dual-write shim, then cutover.
- Wire `canon.merge` conflicts into the §7.2 conflict-log negotiation + director UI.
- Snapshot isolation for long-running "as-of" reads under concurrent merges.
- A scheduled `canon.compact` sweep (cron) once retention policy is product-decided.
