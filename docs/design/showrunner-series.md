# Series-Scale Showrunning — DESIGN.md

**Domain owner:** Showrunner (`backend/app/agents/showrunner.py`, the Showrunner
slice of `backend/app/agents/contracts.py`, `backend/app/agents/prompts.py`, and
the new `backend/app/agents/series.py` package).

**Cites:** kinora.md §7 (the crew), §7.2 (the negotiation protocol), §8.1/§8.2/§8.5
(canon graph / episodic / forgetting), §8.6 (preference learning), §10 (prompt
contracts), §11 (model stack & budget).

---

## 0. The problem

Today the Showrunner reasons about **one book**: `plan_production` decomposes a
summary into scenes, and `decide_arbitration` resolves a single conflict with a
3-branch policy (evolve / surface / honor, §7.2). Kinora's bet, though, is *long*
adaptations — and the natural unit beyond a 300-page novel is a **series**: a
trilogy, a saga, a set of volumes that share characters, relationships, themes,
and a continuity that must hold *across books* (Volume 3 must not contradict
Volume 1; a hero's arc must keep evolving; a motif planted in book one must pay
off in book three).

Series-scale showrunning is the layer that lets the same canon-first architecture
survive a multi-volume work. It is, deliberately, almost entirely **pure,
deterministic, unit-testable policy** — exactly like `decide_arbitration`,
`decide_render_mode` (§9.3), and `decide_qa` (§9.5). The expensive model
(`qwen3.7-max`, §11) is called only to *fill prose* (recap narration, a
series-bible synopsis); every *decision* (where an act break falls, which motif
should pay off now, how to weigh arc continuity against dramatic stakes in an
arbitration) is a pure function of structured inputs.

---

## 1. Why pure functions (the house style)

The codebase already separates **creative judgement** (lives in the model) from
**policy** (lives in pure functions co-located with each agent). The Showrunner's
`decide_arbitration` is the template: it takes the conflict + two injected
booleans and returns the chosen option, so all three branches are tested without
a network. Series-scale work follows the same rule:

- Every decision is a `def f(structured_input) -> structured_output` with no I/O.
- The model's role is narrowed to *prose synthesis* (recap text, bible synopsis,
  motif descriptions) behind the existing `BaseAgent.run_json` contract.
- All new contracts are **additive** Pydantic models with the same
  `model_config` discipline (`extra="forbid"` for outputs, `extra="ignore"` for
  model-filled inputs) and `§`-citation docstrings.

This keeps the new surface area cheap to test (no infra, no credits —
`KINORA_LIVE_VIDEO` stays OFF throughout) and swappable behind the typed-contract
guarantee of §7.1.

---

## 2. Architecture

```
                         ┌──────────────────────────────────────────────┐
                         │            Showrunner (qwen3.7-max)            │
                         │  plan_production · arbitrate (§7.2)            │
                         │  + series methods (prose synthesis only)      │
                         └───────────────┬──────────────────────────────┘
                                         │ calls pure policy
                ┌────────────────────────┴───────────────────────────────┐
                │      app/agents/series/  (PURE package, no I/O)          │
                │                                                          │
                │  arcs        : advance_arc, arc_state_at, build_arc      │
                │  pacing      : tension_curve, pacing_score,              │
                │                worst_pacing_window, smooth/optimize      │
                │  structure   : detect_act_boundaries,                    │
                │                detect_episode_boundaries                 │
                │  recap        : select_recap_beats, build_recap_spec     │
                │  motifs      : plan_motif_callbacks, due_callbacks       │
                │  arbitration : weigh_arbitration (richer §7.2 model)     │
                │  bible       : build_series_bible, continuity index      │
                │  continuity  : cross-volume contradiction detection      │
                │  planner     : replan optimization loop                  │
                │  eval        : arc/pacing/motif coherence metrics (§13)  │
                └──────────────────────────────────────────────────────────┘
                                         │ structured contracts
                ┌────────────────────────┴───────────────────────────────┐
                │   app/agents/contracts.py  (additive series models)      │
                └──────────────────────────────────────────────────────────┘
```

### How it sits in the existing system

- **Canon graph (§8.1)** is per-entity and per-book today. The **SeriesBible** is
  a thin *cross-book* index that references existing canon entity keys (e.g.
  `char_elsa_001`) — it does not duplicate appearance/state data; it records the
  series-level facts the per-book canon can't: which volumes an entity spans,
  each character's *arc* across volumes, relationship trajectories, the thematic
  motifs and their planned callbacks.
- **Episodic store (§8.2)** already stamps every shot with `scene_id`/`beat_id`
  and QA scores. Arc beats and tension points are computed *from* exactly the
  structured signals a scene plan + canon already expose (entity presence, mood,
  page span), so the curves don't need new ingest — they are a *read model*.
- **Arbitration (§7.2)** keeps the existing 3-branch `decide_arbitration` as the
  authoritative gate. The richer `weigh_arbitration` is a **scoring layer that
  feeds it**, never overriding the hard §7.2 invariant (no evolve without
  textual support). `decide_arbitration` is extended *additively* (a new optional
  keyword) so all existing callers and tests keep working unchanged.
- **`ConflictResolver` (`app/render/conflict.py`)** is owned by another domain;
  no edits are made there. The richer arbitration is exposed on the Showrunner so
  that layer can opt into it later through the unchanged `arbitrate` seam.

---

## 3. Implementation status (living)

| Phase | Subsystem | Status |
|---|---|---|
| M1 | Additive series contracts | DONE |
| M1 | `series/arcs.py` — arc tracking + time-travel resolution | DONE |
| M1 | `series/pacing.py` — tension curve + pacing score | DONE |
| M1 | `series/structure.py` — act + episode boundaries | DONE |
| M1 | `series/recap.py` — budget-bounded recap selection | DONE |
| M1 | `series/motifs.py` — motif callback scheduling | DONE |
| M1 | `series/arbitration.py` — richer weighed §7.2 | DONE |
| M1 | Showrunner wiring (back-compatible) + prompts | DONE |
| M2 | `series/bible.py` — build the cross-book bible from volumes | DONE |
| M3 | `series/continuity.py` — cross-volume contradiction detection | DONE |
| M4 | `series/planner.py` — pacing-driven re-plan signals | DONE |
| M6 | `series/eval.py` — arc/pacing/motif coherence metrics (§13) | DONE |
| M4b | `series/inference.py` — arc/tension read model from plan signals (§8.4) | DONE |
| M4c | `series/assembly.py` — end-to-end `assemble_series` → `SeriesProductionPlan` | DONE |
| M7 | `Showrunner.replan_for_pacing` — directive-bounded re-plan round-trip | DONE |
| M5 | Recap rendering + "previously on" UI | ROADMAP (needs render+UI domains) |
| M2b | MCP `series.*` canon-server tools | ROADMAP (needs memory domain) |

---

## 4. Key contracts (all additive)

| Model | Purpose |
|---|---|
| `Volume` | One book within a series; ties a series position to a `book_id`. |
| `ArcStage` (enum) | setup·rising·turn·climax·falling·resolution — shared arc vocabulary. |
| `ArcBeat` | A single sampled point on an arc — the atom curves are built from. |
| `CharacterArc` | A character's evolving arc across volumes. |
| `RelationshipArc` | A relationship's trajectory (allies→rivals, etc.). |
| `ArcState` | The *current* resolved arc-state at a point (the read model). |
| `Motif` | A thematic motif and where it should recur. |
| `MotifCallback` | A scheduled recurrence (plant / echo / payoff). |
| `TensionPoint` | One sample of the narrative-tension curve. |
| `PacingCurve` | The curve the planner optimizes against + derived stats. |
| `ActBoundary` | A detected act break / midpoint. |
| `EpisodeBoundary` | An episode (binge-unit) boundary with cliffhanger flag. |
| `RecapItem` / `RecapSpec` | The "previously on" plan: which prior beats + budget. |
| `SeriesBible` | The cross-book canon index. |
| `ArbitrationContext` | The richer signals weighed during arbitration. |
| `ArbitrationDecision` | The scored, explainable arbitration outcome. |
| `CrossVolumeConflict` | A contradiction between a proposed beat and a prior volume. |
| `ArcCoherenceReport` / `PacingReport` / `MotifReport` | §13 eval metrics. |

`ScenePlan` / `ScenePlanItem` / `DecisionRecord` are **extended additively** with
optional, defaulted fields, so nothing that consumes them today breaks.

---

## 5. Cross-domain contract changes (recorded per the rules)

All changes to the **shared** `backend/app/agents/contracts.py` are **strictly
additive**:

1. New models listed in §4 appended after the existing Showrunner block.
2. `ScenePlanItem` gains optional `volume_index: int = 0`, `act: int | None =
   None`, `tension: float | None = None` (all defaulted).
3. `ScenePlan` gains optional `series_id: str | None = None`, `volume_index: int
   = 0`, `pacing_curve: PacingCurve | None = None` (all defaulted).
4. `DecisionRecord` gains optional `recommended_option: ConflictOption | None =
   None` and `scores: dict[str, float] = {}` (defaulted; `extra="forbid"` still
   holds because they are declared fields).

No field is removed, renamed, retyped, or made required. Every existing
serialization of these models still validates. `decide_arbitration` gains a new
keyword-only `context` argument with a `None` default — backward-compatible.

`backend/app/agents/__init__.py` (owned by no single domain; treated as additive)
re-exports the new `series` symbols.

---

## 6. Roadmap (remaining, phased)

**M5 — Recap rendering + "previously on" UI.** Turn a `RecapSpec` into a
budget-bounded montage through the render pipeline (reusing accepted clips from
episodic memory — a recap costs near-zero new video-seconds, §8.7), and surface a
"Previously on…" card at each volume/episode boundary in the reading room. (Needs
the render + desktop domains.)

**M2b — Bible persistence + MCP `series.*` tools.** Persist `SeriesBible` beside
the §8.1 vault; add MCP tools `series.get_bible` / `series.upsert_arc` /
`series.due_callbacks` (§8.3 surface). (Needs the memory domain.)

**M7 — Live planner feedback.** Wire `replan_for_pacing` into the real
`plan_production` round-trip so a monotonous stretch triggers a model re-plan.

---

## 7. Testing

`backend/tests/test_agents_series.py` covers every pure function with no network
and `KINORA_LIVE_VIDEO` off; the existing `test_agents_showrunner.py` continues to
pass unchanged — proof the additive contract changes broke nothing.

Run: `backend/.venv/bin/pytest tests/test_agents_series.py tests/test_agents_showrunner.py -q`
(70 series tests + 6 single-book showrunner tests, all green; full suite: 515
passed / 145 infra-skipped / 0 failed).

Lint: `ruff check app tests scripts` passes cleanly, and `mypy app` passes on all
161 source files (every owned module included). **Note:** `make lint` reports one
*pre-existing* mypy error — `tests/test_providers_openai_chat.py:45` is missing a
return annotation. That file belongs to the OpenAI reasoning-provider domain
(committed in `5d5e227`), is untouched by this work, and the error reproduces on
the base commit with all of this work stashed. Per the "stay strictly inside your
owned files" rule it is deliberately left for that domain to fix.

### Owned files in this change

* `backend/app/agents/series/` — new package: `arcs.py`, `pacing.py`,
  `structure.py`, `recap.py`, `motifs.py`, `arbitration.py`, `bible.py`,
  `continuity.py`, `planner.py`, `eval.py`, `inference.py`, `assembly.py`,
  `__init__.py`.
* `backend/app/agents/showrunner.py` — series methods + back-compatible
  `decide_arbitration(context=...)` / `arbitrate(context=...)`.
* `backend/app/agents/prompts.py` — additive `SERIES` prompt.
* `backend/app/agents/contracts.py` — additive series models (shared file; see §5).
* `backend/app/agents/__init__.py` — additive `series` re-export (shared file).
* `backend/tests/test_agents_series.py` — the test suite.
* `DESIGN.md` — this document.
