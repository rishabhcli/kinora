# Continuity — the formal continuity-reasoning engine

**Domain owner:** Continuity. Owned files:
`backend/app/agents/continuity.py`, `backend/app/render/continuity_qa.py`,
`backend/app/render/conflict.py`, and the new package
`backend/app/render/continuity_reasoning/` (+ its tests). All work stays inside
these; shared-file changes are **additive only** and documented in
§"Cross-domain contract changes" below.

Cites kinora.md **§7.2** (the structured-conflict negotiation protocol), **§8.5**
(forgetting = continuity versioning over beat intervals), **§9.5** (the Critic's
concrete checks / repair routing), **§10** (prompt contracts; the Critic's
"don't be charitable" discipline, extended to spoilers).

---

## 1. The problem & the bet

Today Continuity does *single-shot contradiction judgement*: one reasoning-model
call returns "does this contradict?" and a deterministic builder wraps the answer
in a §7.2 `ConflictObject`. That is fragile over a 300-page adaptation — the
model re-evaluates from scratch each shot, has no temporal model of the canon, and
cannot explain *why*.

The bet: **the canon's continuity is a formal temporal structure, so reason over
it formally.** A continuity fact is a `(subject, predicate, object)` triple true
only over a beat interval `[valid_from_beat, valid_to_beat)` (§8.5). Contradiction
is then a *derivable* property — two facts on a functional channel that overlap
in time but disagree — not a vibe. The model's only job shrinks to extracting
*what a shot implies*; the engine proves the rest and emits a **proof trace**.

This is the Track-1 ("self-correcting, self-improving memory") + Track-3
("how agents resolve conflicts") money shot made literal: the user can *watch* the
derivation, not take it on faith (§14).

---

## 2. Architecture

```
app/render/continuity_reasoning/         # PURE, network-free, exhaustively tested
  intervals.py     Allen interval algebra over the beat axis (13 relations)
  composition.py   the Allen 13×13 composition table (computed by definition)
  constraints.py   AllenNetwork — path-consistency over qualitative constraints
  facts.py         Fact model: triple + BeatInterval + slot + epistemic visibility
  timeline.py      CanonTimeline — indexed, queryable temporal model of the canon
  proof.py         ProofStep / ProofTrace / Rule — human-readable derivations
  contradiction.py automatic contradiction detection (canon self-consistency + live)
  epistemic.py     reader-knows vs canon-true; spoiler risks; dramatic irony
  belief.py        reader belief revision — unreliable-narrator / misdirection
  propagation.py   ripple a state change across dependent facts (§8.5)
  spatial.py       teleport detection + prop/wardrobe persistence
  inference.py     multi-hop transitive closure (carried props, travelling party)
  engine.py        ContinuityEngine façade → one verdict over a canon slice

app/agents/continuity.py                 # the thin model-bound shell
  Continuity.check_shot         legacy single-call judgement (kept, fallback)
  Continuity.check_shot_formal  model extracts implied facts → engine proves them
  build_conflict_from_finding   render a ProofTrace into a §7.2 ConflictObject
  run_engine_verdict            drive the pure engine directly (tests/wiring)

app/render/continuity_qa.py              # cross-shot seam QA (owned)
  detect_persistence_drift      narrative wardrobe/setting/lighting/time continuity

app/render/conflict.py                   # §7.2 arbitration wiring (owned)
  propagate_evolution           cascading retirement proposals on evolve_canon
```

**Purity boundary.** Everything under `continuity_reasoning/` imports nothing
from `app.agents` / `app.memory` / `app.providers`. The one adapter is
`fact_from_state_slice` (duck-typed via the `StateLike` Protocol), so a memory
`StateSlice` flows in without the engine depending on the memory layer. This is
what makes the reasoning exhaustively unit-testable with hand-built facts.

### 2.1 The Allen core (`intervals.py`)
`BeatInterval(start, end)` is half-open `[start, end)` (matching the canon's
storage: a fact retired at beat 34 is active at 33, not 34). `end=None` = `+∞`
(open-ended / never retired). `relate()` returns exactly one of the 13 Allen
relations; `inverse()` gives the converse; `OVERLAP_RELATIONS` is the set under
which two facts share a beat. All `<`/`>` temporal logic in the engine routes
through here, so the temporal reasoning is one small proven unit.

### 2.2 Facts & functional channels (`facts.py`)
A `Fact` competes on a **channel** `(subject, predicate, slot)`. A *functional*
predicate (`located_in`, `is`, `possesses`, `wearing`, …) admits at most one
object per beat — two overlapping facts on one functional channel that disagree
is the core contradiction. `possesses`/`holding`/`wearing` are functional *per
slot* (`fact_slot` maps "iron sword" → `weapon`, "lit torch" → `light`), so a
hero may hold a sword *and* a torch without a false clash.

Each fact also carries **epistemic visibility** (`KNOWN` / `HIDDEN` / `MISTAKEN`)
and an optional `revealed_at_beat`, decoupling *canon truth* from *reader
knowledge*.

### 2.3 Proof traces (`proof.py`)
A `ProofTrace` is an ordered chain of `ProofStep`s (`premises ⟹[rule]
conclusion`) plus a one-line `summary` and the `cited_fact_ids` to highlight.
Named `Rule`s: `functional_conflict`, `proposed_vs_active`,
`retired_before_beat`, `not_yet_active`, `transitive`, `colocation`,
`epistemic_spoiler`, `reader_misbelief`, `temporal_relation`. `render()` produces
the multi-line text the §7.2 conflict and the agent-activity feed display.

### 2.4 The reasoners
- **`contradiction.py`** — `detect_canon_contradictions` audits a whole timeline
  for functional-channel clashes (catches a mis-asserted canon before it poisons
  generation). `check_proposed_fact` is the live path: a shot's implied fact vs.
  the active canon, including the §8.5 *retired-before-beat* case ("draws a sword
  retired at beat 34").
- **`epistemic.py`** — `reader_knowledge_at` splits canon-true from reader-known;
  `spoiler_risks` / `check_spoiler` flag depicting a reveal early;
  `dramatic_irony_beats` lists where the reader knows less than the canon.
- **`propagation.py`** — `propagate_retirement` ripples a lost prop to its
  dependents (possession links → `RETIRE`, looser references → `REVIEW`);
  `propagate_supersede` closes the prior value when a new functional fact lands.
  Proposes closures only — §8.5 keeps the stale fact for time-travel reads.
- **`spatial.py`** — `detect_spatial_conflicts` (teleport: one entity in two
  places at one beat), `colocated_at` (co-presence set), `prop_persistence_gaps`
  (a shot still showing a retired prop).
- **`inference.py`** — `transitive_location` (a carried prop is where its holder
  is), `multi_hop_closure` (a travelling party shares a location, then their
  props too), with a hop cap so a cyclic `accompanied_by` graph terminates.

### 2.5 The façade & the agent (`engine.py`, `continuity.py`)
`ContinuityEngine.from_state_slices(...)` builds a timeline from a `canon.query`
active-state list. `check_shot_claims(queries)` proves each implied fact
(contradictions before spoilers) → one `ContinuityVerdict` whose `primary`
finding carries the proof. `audit_canon()` runs the whole-canon self-consistency
pass. `with_inference_at(beat)` enriches the timeline with multi-hop facts so a
*composed* contradiction is caught.

`Continuity.check_shot_formal` runs the model's fact-extraction (`ContinuityClaims`
/ `ImpliedFact`) then the engine; on a finding it builds a §7.2 conflict whose
`canon_fact` is the rendered proof trace and whose `contradicting_state_id` cites
the exact canon fact. It falls back to the legacy `check_shot` when no concrete
facts are extracted, so a vague depiction is never silently approved.

---

## 3. Integration points

- **Conflict arbitration (`app/render/conflict.py`, §7.2):** the
  `ContinuityChecker` Protocol (`check_shot`) already fits; `check_shot_formal`
  has the same signature shape and is a drop-in upgrade. The proof trace now
  flows through `ConflictObject.canon_fact` → the Showrunner's
  `judge_textual_support` sees the derivation, and `evolve_canon` re-asserts the
  cited fact. (Phase 3 wires a `ProofTracingResolver` — see roadmap.)
- **Memory (`app/memory`, §8.5):** `StateLike` is the read seam; a future write
  seam (`propose_retirements`) would feed `propagate_*` effects to
  `canon.retire_state`. No memory code changed.
- **Critic (`app/agents/critic.py`, §9.5):** a `timeline_ok=false` verdict routes
  `raise_conflict` → Continuity; the formal path supplies the proof the Critic
  cannot (it sees one clip, not the canon's temporal structure).

---

## 4. Phased roadmap

| Phase | Scope | Status |
|---|---|---|
| **1. Temporal core + contradiction + proof traces** | Allen algebra, Fact/Timeline, `detect_*` / `check_proposed_fact`, proof traces | **DONE** |
| **1b. Epistemic + propagation + spatial + multi-hop** | reader-knowledge, spoilers, ripple, teleport, prop persistence, transitive closure | **DONE** |
| **2. Agent integration** | `check_shot_formal`, `build_conflict_from_finding`, `run_engine_verdict`; legacy path preserved | **DONE** |
| **3. Evolve-canon propagation** | `propagate_evolution` in `conflict.py` → cascading `RetirementProposal`s the canon-write seam applies (the §8.5 "close the old fact" write) | **DONE** |
| **4. Constraint network** | Allen path-consistency + a composition table *computed by definition* (self-checked, incl. the converse law); `engine.audit_temporal_consistency` detects implied cycles (A<B<C<A) | **DONE** |
| **5. QA-seam continuity** | `continuity_qa.detect_persistence_drift` — unmotivated wardrobe/setting/lighting/time drift across chained seams | **DONE** |
| **6. Belief revision** | `belief.py`: `ReaderBelief` / `BeliefState` model false reader beliefs, render-target check, dramatic-irony detection, and the reveal-time `BeliefRevision` | **DONE** |
| **7. Canon-edit audit endpoint** | `audit_canon` / `audit_temporal_consistency` surfaced on a route so a Director edit is checked before it ships | engine ready; route is another domain |
| **8. Persistence of derived facts** | cache `multi_hop_closure` per beat in episodic memory so repeated beats don't recompute | planned |
| **9. Resolver swap to formal path** | flip `ConflictResolver` to call `check_shot_formal` (drop-in: same `ContinuityChecker` shape) once the extraction prompt is tuned | planned |
| **10. Spoiler-aware Cinematographer prior** | feed `spoiler_risks(beat)` to the Cinematographer so a shot never *plans* to depict a reader-unknown reveal | planned (cross-domain) |

---

## 5. Cross-domain contract changes (additive only)

**None to shared files.** All new code lives in owned files:
- new package `backend/app/render/continuity_reasoning/` (entirely new, owned);
- `backend/app/agents/continuity.py` — additive: new `ImpliedFact`,
  `ContinuityClaims` models, `check_shot_formal`, `build_conflict_from_finding`,
  `run_engine_verdict`; the existing `Continuity.check_shot` / `build_conflict` /
  `format_state` signatures are unchanged (back-compat preserved);
- `backend/app/render/continuity_qa.py` — additive: `PersistenceDrift`,
  `PersistenceReport`, `detect_persistence_drift`; all existing seam-scoring
  functions unchanged;
- `backend/app/render/conflict.py` — additive: `RetirementProposal`,
  `propagate_evolution`; the `ConflictResolver` flow unchanged;
- new test files `tests/test_continuity_reasoning.py`,
  `tests/test_render_conflict_propagation.py`, and additive cases in
  `tests/test_agents_continuity.py` / `tests/test_render_continuity_qa.py`.

No edits to `app/agents/contracts.py`, `app/memory/interfaces.py`, or any other
domain's files. The reasoning engine reads memory `StateSlice` rows through the
duck-typed `StateLike` Protocol (declared *inside* the owned package), so it adds
**no** import dependency on the memory layer.

**One pre-existing, non-owned lint failure** is unrelated to this work:
`tests/test_providers_openai_chat.py:45` (missing return annotation), introduced
by commit `5d5e227` (the OpenAI reasoning-provider merge). Left untouched — it is
another domain's file. All owned files are ruff + mypy clean.

Should a future phase need a new `ConflictType` (e.g. an explicit `SPOILER`) or a
canon write-seam Protocol, those will be **additive** entries appended to the
shared enums/Protocols and recorded here before merge. Today the spoiler finding
maps onto the existing `ConflictType.TIMELINE_CONTRADICTION`.

---

## 6. Test status

`tests/test_continuity_reasoning.py` (29 tests) — Allen algebra exhaustiveness +
inverse identity, half-open membership, the §8.5 retired-sword contradiction with
proof, functional clashes, canon self-consistency, epistemic spoilers + dramatic
irony, propagation (retire/supersede), spatial teleport + prop persistence,
multi-hop closure (incl. cyclic-accompaniment termination), and the engine façade.

`tests/test_agents_continuity.py` (+5 formal-path tests) — model extracts implied
facts → engine proves them → proof-traced §7.2 conflict; consistent-depiction
pass; empty-extraction fallback; `build_conflict_from_finding`.

Owned files are ruff + mypy clean. Full suite: **484 passed, 145 skipped** (infra
& live-video gated), no regressions. `KINORA_LIVE_VIDEO` stays OFF.
