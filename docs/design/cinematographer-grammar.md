# Cinematographer — Full Cinematic-Language Model (DESIGN.md)

**Domain owner:** Cinematographer agent + render-side film grammar.
**Owned files:**
- `backend/app/agents/cinematographer.py`
- `backend/app/render/shot_grammar.py`
- `backend/app/render/segment_packer.py`
- `backend/app/render/cinematic_language.py` *(new — created in this domain)*
- `backend/tests/test_render_cinematic_language.py` *(new)*
- `backend/tests/test_agents_cinematographer.py`, `backend/tests/test_render_shot_grammar.py` *(extended)*

**Authoritative spec:** `kinora.md` §9.3 (Wan-mode decision tree), §7.1 (typed
Cinematographer contract), §8.6 (preference learning), §10 (prompt contracts).

---

## 1. The bet

Today the Cinematographer picks a render mode with the 6-branch §9.3 tree
(`decide_render_mode`) then asks the LLM to "fill creative content." That makes
each shot a *fresh improvisation*: nothing guarantees a noir book is shot like
noir, that the lens stays consistent, that two characters' eyelines match across
a cut, or that the scene has a deliberate cutting rhythm.

This domain makes the **language of the image deterministic**. The same way the
§9.3 tree turned mode-selection into pure, testable code, the cinematic-language
model turns *lens / lighting / colour-grade / coverage / blocking / eyelines /
cadence / directorial-eye* into pure functions of text the Adapter already
produced. The LLM then fills prose **within a fixed look** instead of
re-imagining the film every five seconds.

Everything is a pure function of `Beat` text + canon style tokens. No network, no
model call, no invented reference id — exactly the §9.3 contract.

---

## 2. Architecture

```
                       ┌──────────────────────────────────────────┐
                       │  app.render.cinematic_language (NEW)       │
                       │                                            │
   Beat text  ───────► │  infer_genre / infer_mood                  │
   style tokens ─────► │      │                                     │
                       │      ▼                                     │
                       │  select_style_profile ─► StyleProfile      │  the directorial
                       │      │   (7 emulation profiles)            │  EYE (one per film)
                       │      ▼                                     │
                       │  lens_for / lighting_for / color_grade_for │  the LOOK
                       │  block_subjects / shot_reverse_shot        │  COMPOSITION
                       │  shot_length_cadence                       │  RHYTHM
                       │  plan_coverage                             │  EDITORIAL set
                       │      │                                     │
                       │      ▼                                     │
                       │  plan_scene ─► ScenePlan { shots,          │  one source of truth
                       │                 coverage, axis_violations }│
                       └───────┬────────────────────────┬───────────┘
                               │                        │
              build_brief /    │                        │  detect_axis_violations
        build_segment_brief    ▼                        ▼  eyeline_consistent
                       ┌────────────────────┐   ┌────────────────────────┐
                       │ Cinematographer     │   │ app.render.shot_grammar │
                       │ .design_shot        │   │ (EXTENDED, additive)    │
                       │ .design_segment     │   │  + AxisViolation        │
                       │  → cinematography    │   │  + detect_axis_violations│
                       │    block in payload  │   │  + eyeline_consistent   │
                       └────────────────────┘   └────────────────────────┘
                                                   (also read by continuity_qa,
                                                    event_director — unchanged)
```

### Layers of the model

| Layer | Function(s) | What it decides |
|---|---|---|
| Genre/mood vocabulary | `infer_genre`, `infer_mood` | Coarse genre per scene; emotional register per beat |
| Director-style emulation | `STYLE_PROFILES`, `select_style_profile` | The one directorial eye the film is shot through |
| Lens/lighting/grade | `lens_for`, `lighting_for`, `color_grade_for` | The look, derived from the eye + nudged by the beat |
| Coverage | `plan_coverage` | The master / medium / CU set a scene is cut from |
| Shot/reverse-shot | `shot_reverse_shot` | Eyeline-matched two-hander singles on one side of the line |
| Blocking | `block_subjects` | Which third of frame the subject sits in (+ lead room) |
| Visual rhythm | `shot_length_cadence` | The shot-length pattern (tightening as it builds) |
| Axis tracking | `detect_axis_violations`, `eyeline_consistent` *(in shot_grammar)* | Stateful 180° rule across an event |
| Sequencer | `plan_scene` → `ScenePlan` | All of the above, one beat at a time, plus coverage + violations |

### Style profiles (the emulation library)

Seven coherent directorial eyes, each a bundle of lens + lighting + grade +
camera temperament + symmetry. Names describe a *look*, not a person:
`anamorphic_symmetry`, `naturalistic_handheld`, `noir_chiaroscuro`, `epic_vista`,
`romantic_soft`, `kinetic_action`, `classical_balanced` (the default).

Selection precedence (deterministic): explicit `override` → canon style token
(`director_style`/`aesthetic`/`profile`/`look`) → genre default → classical.

---

## 3. Contracts

### New types (all in `app.render.cinematic_language`, pure dataclasses/enums)
- `Genre`, `Mood` (StrEnum)
- `StyleProfile` (frozen) + `STYLE_PROFILES: dict[str, StyleProfile]`
- `CoverageRole` (StrEnum), `CoverageShot` (frozen)
- `ReverseShotPair` (frozen)
- `FramePosition` (StrEnum), `Blocking` (frozen)
- `Cadence` (frozen)
- `ShotPlan` (frozen), `ScenePlan` (frozen) — the sequencer output

### New types in `app.render.shot_grammar` (additive)
- `AxisViolation` (frozen) — one unmotivated 180° flip
- `detect_axis_violations(beats) -> list[AxisViolation]`
- `eyeline_consistent(a_looks, b_looks) -> bool`

### Prompt + grammar compilers (`app.render.cinematic_language`)
- `compile_shot_prompt(plan, *, subject=None) -> str` / `compile_scene_prompts`
  — turn a `ShotPlan` into the deterministic Wan/i2v prompt clause.
- `negative_prompt_for(beats, *, genre=None) -> str` — base artifact floor +
  per-genre look-breakers, de-duped.
- `expressive_move_for(beat, genre) -> str | None` + `move_phrase(move, speed)`
  — the (genre, mood) → expressive-move vocabulary (dolly-zoom, whip-pan, …).
- `LookJump` / `LookJumpKind` / `detect_look_jumps(plan)` — unmotivated
  lens/grade discontinuity guard.

### Cinematographer agent (`app.agents.cinematographer`)
- `CinematicBrief` (frozen, fields: `profile`, `genre`, `lens`, `lighting`,
  `grade`, `negative_floor`) + `.payload()` — the `cinematography` block.
- `build_brief(beat, canon_slice, *, profile_override=None) -> CinematicBrief`
- `build_segment_brief(beats, canon_slice, *, profile_override=None) -> CinematicBrief`
- `design_shot` / `design_segment` now inject `payload["cinematography"]` into the
  LLM request AND union the deterministic `negative_floor` into the resulting
  `ShotSpec.negative_prompt` (`_merge_negative`: the LLM may add to the floor,
  never drop it). **No change to the `ShotSpec` / `CinematographerFill` wire
  schema** — the brief is request-side context and the negative floor is merged
  into the existing field, so the contract in `app.agents.contracts` is untouched
  and every downstream consumer is unaffected.

The deterministic camera floor and the §8.6 priors still apply exactly as before
(`_apply_priors`): the brief is the *default look*, priors and explicit director
notes still win on the axes they speak to.

---

## 4. Integration

- **Cinematographer agent → LLM:** `design_shot`/`design_segment` add a
  `cinematography` block (directorial eye + genre + lens/lighting/grade/look) to
  the request payload. Purely additive; existing tests still pass.
- **shot_grammar consumers:** `continuity_qa.py` (imports `ScreenDirection`,
  `violates_180`) and `event_director.py` (imports `resolve_screen_directions`,
  `shot_size_for`, `is_motion_reversal`) are untouched and still green — the new
  `AxisViolation` / `detect_axis_violations` / `eyeline_consistent` are additive.
- **Future wiring (not yet done, see roadmap):** `event_director.plan_event_script`
  could consume `plan_scene` directly so the event film and the cinematic plan
  share one source of truth, and `continuity_qa` could call
  `detect_axis_violations` to raise 180° seams as a structured repair.

---

## 5. Cross-domain notes

- **Additive-only on shared files:** `app.agents.contracts` was **not** modified
  (the brief rides in the request payload, not the response schema). If a later
  phase wants the brief persisted on `ShotSpec`, that is an additive optional
  field and must be coordinated with the agents-contract owner.
- **Pre-existing lint failure (NOT this domain):** `make lint` (mypy) fails on
  `backend/tests/test_providers_openai_chat.py:45` — `Function is missing a
  return type annotation` on `_openai_client`. This is committed at HEAD
  (`5d5e227`, the OpenAI reasoning-provider work) and lives in the providers
  domain, so it is left untouched per the "never edit other domains' code" rule.
  All files in **this** domain pass `ruff` + `mypy` cleanly, and the full pytest
  suite is green (see §7). The owner of the providers domain should add
  `-> AsyncOpenAIClient` (or the correct type) to that test helper.

---

## 6. Phased roadmap

### Done (this run)
- **Phase 1 — Cinematic-language core.** `cinematic_language.py`: genre/mood,
  7 style-emulation profiles, lens/lighting/grade derivation, coverage planning,
  shot/reverse-shot eyeline matching, blocking, cadence, per-beat camera.
  25 tests.
- **Phase 2 — Agent wiring.** `CinematicBrief` + `build_brief`/`build_segment_brief`;
  `design_shot`/`design_segment` inject the `cinematography` block. Brief tests.
- **Phase 3 — Axis tracking + continuity primitives.** `shot_grammar.py`
  extended additively with `AxisViolation`, `detect_axis_violations`,
  `eyeline_consistent`. Tests added; existing consumers still green.
- **Phase 4 — Scene sequencer.** `plan_scene` → `ScenePlan`/`ShotPlan` ties every
  layer into one deterministic, serializable plan (shots + coverage + 180°
  violations). Tests added.
- **Phase 7 — Lens/grade continuity guard.** `LookJump`/`detect_look_jumps`:
  flags an unmotivated focal-length pop or grade jump (a change with no size or
  mood motivation). Tests added.
- **Phase 8 — Prompt-fragment compiler.** `compile_shot_prompt`/
  `compile_scene_prompts`: a `ShotPlan` → deterministic Wan/i2v prompt clause
  (framing + blocking + move + lens + lighting + grade + lead room). Tests added.
- **Phase 9 — Negative-prompt + expressive-move grammar.** `negative_prompt_for`
  (base floor + per-genre look-breakers), `expressive_move_for`/`move_phrase`
  (dolly-zoom, whip-pan, gentle-orbit, …), wired into `plan_scene`. The agent now
  merges the negative floor into every `ShotSpec`. Tests added.
- **Phase 10 — Style-note → eye (the §8.6 cinematographer-side bridge).**
  `infer_style_override` maps a free-text director note that names a *look*
  ("shoot it like noir", "more symmetrical") to a profile id; the agent's
  `style_override_from_notes` feeds it into the brief so a style ask re-shoots the
  scene through a different eye (axis asks like "slower" stay on the §8.6 prefs
  path). Tests added.
- **Phase 11 — Transition grammar.** `Transition`/`transition_between`/
  `plan_transitions`/`transition_seconds`: text-motivated cut/dissolve/fade/
  match-cut/smash-cut between moments (the seam the stitcher's crossfade reads).
  Tests added.

### Remaining (future runs)
- **Phase 5 — Event-director adoption.** Make `event_director.plan_event_script`
  build from `plan_scene` so the event film inherits the directorial eye, lens,
  cadence, blocking, and transitions instead of its own ad-hoc camera logic.
  Requires coordination (event_director is render-domain but a different
  work-stream).
- **Phase 6 — Continuity-QA adoption.** `continuity_qa` raises
  `detect_axis_violations` / eyeline mismatches / `detect_look_jumps` as
  structured `SeamRepair`s.
- **Phase 12 — Persistent director-style prior (§8.6 memory tie-in).** A repeated
  style ask learns a per-book `director_style` prior on the memory side (the
  prefs domain owns that write-path) that feeds `select_style_profile`'s
  `style_tokens` — coordinate with the memory-domain owner.
- **Phase 13 — Multi-character blocking (3+), depth staging, and
  foreground/midground/background occupancy planning.**
- **Phase 14 — J/L audio-led transitions and match-cut shape detection from the
  beat's described visuals.**

---

## 7. Verification

- Owned-domain lint: `ruff` + `mypy` clean on all four owned modules + the three
  test files.
- Domain tests: `test_render_cinematic_language.py` (31), `test_agents_cinematographer.py`
  (extended), `test_render_shot_grammar.py` (extended), `test_render_segment_packer.py`.
- Full suite: `backend/.venv/bin/pytest -q` → **480+ passed, 145 skipped
  (infra-gated), 0 failed** with this domain's additions.
- `make lint` is red **only** on the pre-existing providers-domain mypy error
  documented in §5 — not on any file this domain touches.
