# Adapter — Deep Literary-Comprehension Engine (DESIGN)

Owner domain: `backend/app/agents/adapter.py`, `backend/app/agents/comprehension/**`
(new), and the page-analysis / shot-planning in `backend/app/ingest/analyze.py`
and `backend/app/ingest/shot_plan.py`. Cited spec: **kinora.md §4.2, §9.1, §10.**

The Adapter used to do a single-pass page→beat→shot decomposition with simple
entity resolution and word-count durations. This work turns it into a **deep
literary-comprehension engine**: multi-POV + unreliable-narrator handling,
non-linear timeline reconstruction (narrative-time vs story-time),
free-indirect-discourse + interiority detection, dialogue attribution + speaker
diarization, literary-device → visual-intent translation, and pacing-aware beat
segmentation that varies shot density by scene tempo — while keeping the
deterministic beat→shot split a **pure function** (the §4.2 testability rule).

---

## 1. Architecture

```
            ┌──────────────────────── Adapter (agent) ───────────────────────┐
 page text ─┤ analyze_page  ── LLM (page→beats) ──► per-beat comprehension    │
            │ comprehend_sequence ── pure book-level pass (story-time)        │
            │ plan_shots    ── PURE, pacing-aware beat→shot split             │
            └──────────────┬──────────────────────────────────────────────────┘
                           │ composes
        ┌──────────────────▼───────────────────────────────────────────────┐
        │  app.agents.comprehension  (PURE, network-free passes)            │
        │  ├─ text_utils  sentence split · quote extraction · tokenizer     │
        │  ├─ dialogue    attribution + speaker diarization (§10 no-invent)  │
        │  ├─ pov         person + focal char + unreliable-narrator flag     │
        │  ├─ discourse   dialogue / interior / free-indirect / narration    │
        │  ├─ devices     simile·metaphor·personification·symbol → visual    │
        │  ├─ pacing      SceneTempo + shot-density + duration bias          │
        │  ├─ timeline    narrative-time → story-time reconstruction         │
        │  └─ engine      analyze_beat · enrich_sequence · build_shot_intent  │
        └────────────────────────────────────────────────────────────────────┘
```

**Why a separate package under `app.agents` (not `app.ingest`):** the engine is
the Adapter's *literary brain* and depends only on `app.agents.contracts`.
Putting it under `app.ingest` created a real import cycle
(`agents.adapter → ingest/__init__ → ingest.service → agents.adapter`). Homing it
at `app.agents.comprehension` removes the cycle and matches ownership (it is the
Adapter agent's logic).

**Two-phase design.** Per-beat passes (`analyze_beat`) are order-free and run
page-by-page inside `analyze_page`. Cross-beat structure (story-time
reconstruction) needs the whole ordered sequence — flashbacks span pages — so it
is a second pass (`enrich_sequence` / `Adapter.comprehend_sequence`) the ingest
phase calls once after all pages are segmented.

**Determinism.** Every pass is heuristic + lexical and network-free, so the
entire engine is unit-testable without a model call (the §10 discipline). An LLM
enrichment pass can later layer on top and override any individual field; the
deterministic engine is the always-available floor.

---

## 2. Contracts (additive only on the shared `app.agents.contracts`)

All changes are **purely additive** with neutral defaults, so any beat produced
by the legacy single-pass path remains valid and every existing consumer is
unaffected.

New enums: `NarrativePerson`, `DiscourseMode`, `SceneTempo`, `TimePosition`.
New value objects: `DialogueLine`, `LiteraryDevice`, `StoryTime`, `ShotIntent`.

`Beat` gains (all optional, default-neutral):
`pov`, `pov_character`, `unreliable`, `discourse`, `interiority`, `dialogue`,
`devices`, `tempo`, `story_time`.

`ShotListItem` gains `intent: ShotIntent` (default-empty) — the
comprehension-derived staging brief the Cinematographer conditions on.

These are documented as the **cross-domain contract changes** below.

---

## 3. The passes (what each decides, and the signal it keys on)

| Pass | Output | Key signals |
|---|---|---|
| `dialogue` | attributed `DialogueLine[]` | dialogue tags (`said X` / `X said`), nearest proper name, two-party alternation; canon-filtered (no invented speakers) |
| `pov` | `NarrativePerson` + focal char + `unreliable` | pronoun share in the *narration* (speech stripped); focal = dominant subject of interiority verbs; unreliability = hedging / deception / irony cues |
| `discourse` | `DiscourseMode` + `interiority` | dialogue density; explicit thought tags / 1st-person present (interior monologue); rhetorical questions + colloquial colouring in 3rd-person past (free indirect) |
| `devices` | `LiteraryDevice[]` w/ `visual_intent` | simile (`like/as a …`), copular metaphor (`X was a Y`), personification (inanimate subject + animate verb), loaded symbols — each → a concrete staging instruction, **never a new canon entity** |
| `pacing` | `SceneTempo` + density + bias | long-span markers → SUMMARY; time-jump markers → ELLIPSIS; dialogue/action → SCENE; held description → PAUSE |
| `timeline` | `StoryTime` (narrative vs story order) | back-cues (`years before`, `remembered`, past-perfect) → FLASHBACK ranked earlier; forward-cues (`would later`) → FLASHFORWARD ranked later; explicit resume closes a block |

**Non-linear timeline reconstruction** is the subtle one: narrative-order is
preserved exactly (it is the scroll-sync key the source-span index sorts on),
while `story_time.order` gives a chronological rank so a consumer can replay
events in story order. A contiguous flashback keeps its internal order as a block
anchored just before the present moment it recalls; past-perfect framing keeps an
open block open until an explicit return to the now.

**Pacing-aware split (still PURE).** `plan_shots` reads each beat's `tempo`:
`words_per_shot_for(tempo)` sets how many narration words one shot covers (SCENE
keeps the 60-word baseline; SUMMARY/ELLIPSIS pack a long span into a single clip),
and `duration_bias(tempo)` biases per-shot screen-time (a PAUSE lingers). A
neutral `SceneTempo.SCENE` beat reproduces the legacy split **exactly** (verified
by `test_plan_shots_scene_matches_legacy`).

---

## 4. Integration

- `Adapter.analyze_page(..., comprehend=True)` — per-beat comprehension folded
  into the existing LLM page pass; `comprehend=False` opts out (legacy beats).
- `Adapter.comprehend_sequence(beats, known_entities=…)` — book-level pass
  (per-beat + story-time).
- `ingest/shot_plan.py::plan_and_persist` — after span reconciliation, runs
  `adapter.comprehend_sequence` over the whole book so story-time (cross-page)
  and pacing tempo are resolved before persistence and the pacing-aware
  `plan_shots`. Fully within my domain; no schema change required for the shot
  plan itself.
- `plan_shots` attaches each beat's `ShotIntent` to its shots, ready for the
  Cinematographer.

Verified end-to-end against an **isolated** Postgres
(`kinora_comprehension_test` on host port 5433, never the live `kinora` DB):
`plan_and_persist` + `resolve_word_to_shot` still give full, gap-free global
coverage with the comprehension wired in.

---

## 5. Cross-domain contract changes (ADDITIVE — recorded per the rules)

File `backend/app/agents/contracts.py` (shared). All additive, default-neutral:

1. New enums `NarrativePerson`, `DiscourseMode`, `SceneTempo`, `TimePosition`.
2. New models `DialogueLine`, `LiteraryDevice`, `StoryTime`, `ShotIntent`.
3. `Beat` new optional fields: `pov`, `pov_character`, `unreliable`, `discourse`,
   `interiority`, `dialogue`, `devices`, `tempo`, `story_time`.
4. `ShotListItem` new optional field `intent: ShotIntent`.
5. `__all__` extended with the above names.

File `backend/app/agents/prompts.py` (shared). Additive only:

6. New `ADAPTER_COMPREHEND` versioned prompt + `ADAPTER_COMPREHEND_PROMPT_VERSION`,
   registered in `PROMPTS["adapter_comprehend"]` and `__all__`. Existing prompts
   untouched.

File `backend/app/ingest/shot_plan.py` (owned). `ShotPlanResult` gains
`comprehension: ComprehensionReport` (default-empty) — additive telemetry.

No existing field changed type or default; `extra="ignore"`/`"forbid"` configs
unchanged. Other agents (Cinematographer/Continuity/Critic/Showrunner) read the
new fields opportunistically or ignore them — none are required to.

> **Note for the Cinematographer owner:** `ShotListItem.intent` and the new
> `Beat` literary fields are available to condition the shot-design prompt
> (subjective vs literal framing, POV vantage, device motifs, pacing). Consuming
> them is optional and non-breaking.

---

## 6. Roadmap (done vs remaining)

### Done (this run)
- [x] **Phase 0 — contracts:** additive `Beat`/`ShotListItem` fields + new
      enums/value objects.
- [x] **Phase 1 — comprehension engine:** seven pure passes (`text_utils`,
      `dialogue`, `pov`, `discourse`, `devices`, `pacing`, `timeline`) + the
      composing `engine` (`analyze_beat`, `enrich_sequence`, `build_shot_intent`).
      63 unit tests.
- [x] **Phase 2 — Adapter integration:** `analyze_page` comprehends each beat;
      `comprehend_sequence` adds cross-page story-time; `plan_shots` is
      pacing-aware while staying pure (legacy SCENE behaviour preserved).
- [x] **Phase 2b — ingest wiring:** `plan_and_persist` runs the book-level
      comprehension; verified on an isolated DB.
- [x] **Phase 3a — cinematography bridge:** `ShotIntent` + `build_shot_intent`
      surface comprehension as a structured staging brief on every shot.
- [x] **Phase 4 — LLM enrichment pass.** `Adapter.enrich_beat_llm` runs a
      bounded JSON-strict refinement (`ADAPTER_COMPREHEND` prompt) over the
      deterministic floor; `comprehension/llm.py` holds the pure, canon-guarded,
      conservative `merge_comprehension` (unit-tested offline with canned JSON,
      graceful fallback on a bad reply). The heuristic floor is always the
      fallback — the LLM can only improve, never regress.
- [x] **Phase 3c — comprehension telemetry.** `comprehension/report.py`
      aggregates a sequence into a `ComprehensionReport` (POV distribution +
      multi-POV flag, timeline linearity / flashback counts, tempo + discourse
      histograms, dialogue/device counts); surfaced on `ShotPlanResult` and
      logged at ingest.

### Remaining (future phases)
- [ ] **Phase 3b — persist comprehension to the canon (shared DB; needs a
      migration).** Add an additive `comprehension JSONB` column (or columns:
      `pov`, `tempo`, `story_order`, …) to the `beats` table + Alembic migration,
      and round-trip it through `BeatRepo` and `Adapter._beat_from_row` so
      `plan_scene` is pacing-aware on persisted beats too. Owned files:
      `db/models/beat.py`, `db/repositories/beat.py`, a new Alembic revision —
      these are **shared**, so coordinate / additive-only.
- [ ] **Phase 5 — story-time consumers.** A "story-order recap" surface and a
      Scheduler hint so a flashback's *speculative keyframe* can borrow the canon
      state as-of its story-time, not its narrative-time.
- [ ] **Phase 6 — multi-POV canon scoping.** Tie `pov_character` into the canon
      retrieval slice so a limited-POV beat only sees what that character knows
      (interacts with §8.4 retrieval — coordinate with the memory domain).
- [ ] **Phase 7 — discourse → render-mode hints.** Feed `discourse`/`intent`
      into the §9.3 render-mode tree (subjective beats favour stylised modes).
      Lives in the Cinematographer domain; expose via `intent` (done) so it is a
      pull, not a push.
- [ ] **Phase 8 — segment packing awareness.** Make `segment_packer` respect
      tempo + story-time block boundaries so a single ≤15s take never straddles a
      flashback seam (render domain; pull from the new fields).
- [ ] **Phase 9 — multilingual + verse.** Extend the heuristics beyond English
      prose (quote families partly done) and handle poetry/verse line-breaks.

---

## 7. Test inventory

- `tests/test_comprehension_text_utils.py` — sentence split, quotes, strip.
- `tests/test_comprehension_dialogue.py` — tags, alternation, canon filter.
- `tests/test_comprehension_pov.py` — person, focal, unreliable narrator.
- `tests/test_comprehension_discourse.py` — dialogue/interior/free-indirect.
- `tests/test_comprehension_devices.py` — simile/metaphor/personification/symbol.
- `tests/test_comprehension_pacing.py` — tempo + density + bias.
- `tests/test_comprehension_timeline.py` — flashback/forward story-order.
- `tests/test_comprehension_engine.py` — composition + `ShotIntent`.
- `tests/test_comprehension_llm.py` — LLM merge policy + Adapter `enrich_beat_llm`.
- `tests/test_comprehension_report.py` — book-level telemetry aggregation.
- `tests/test_agents_adapter_comprehension.py` — Adapter integration, pacing.
- (existing) `tests/test_agents_adapter.py`, `tests/test_ingest_shot_plan.py` —
  unchanged, still green (regression).

Run: `cd backend && .venv/bin/pytest tests/test_comprehension_*.py
tests/test_agents_adapter*.py tests/test_ingest_shot_plan.py -q`.
DB-bound: prefix `KINORA_TEST_DATABASE_URL=…/kinora_comprehension_test`.
