# 10-Book Story-Accuracy & Video-Sync QA Campaign — design

Date: 2026-07-04
Status: approved (design), self-reviewed, proceeding directly to execution per
user instruction
Scope owner: backend render/agent pipeline (`backend/`) + desktop reading room
(`apps/desktop`)

## 1. Goal

The ultimate upgrade to how Kinora's agent crew guarantees story accuracy and
scroll-synced video correctness — not an incremental hardening pass. Every
already-built-but-dormant accuracy system in this codebase gets promoted into
the live path; a new whole-book long-range continuity layer gets built where
none existed; the crew gets audited for recurring failure *patterns*, not just
individual defects; and all of it gets proven, meticulously, across 10 full
real novels with durable, committable evidence. Apple-level bar: no broken,
frozen, or silently-wrong state, ever, on any of the 10 books, in any tested
edge case.

**The one constraint that does not move under "ultimate":** the user's real
account balance ($25.44). "Ultimate" is spent on engineering rigor, coverage,
and thoroughness — not on disregarding a specific, factual, stated financial
constraint. Every dollar guardrail in this design is unchanged from that
constraint; everything else is maximized against it.

## 2. Grounding — what's already verified solid (2026-07-04 audit)

Do not rebuild these; build on them:

- **Scroll↔video sync** (`apps/desktop/src/reading/{ScrollFilmEngine.tsx,useScrollFilm.ts}`) —
  a real passive-scroll rAF loop computing fraction/velocity/focus-word, mapping
  to shots via `source_span.word_range`, imperatively driving `<video>`.
- **Live single-shot QA gate** (`backend/app/render/pipeline.py`) — genuinely
  calls the Critic (CCS/style-drift/motion), routes identity/style failures
  through repair, timeline contradictions through Continuity→Showrunner
  arbitration (§7.2), degrades to Ken-Burns on retry exhaustion.
- **MiniMax provider** (`backend/app/providers/minimax.py`) — real submit→poll→
  retrieve→download, `budget_ceiling_usd` guard enforced via `would_exceed_usd`
  before every submission (default $30, Redis-persisted, survives restarts).
- **`kinora-admin books export-review`** (`backend/app/cli/actions/review_export.py`) —
  already exports a reading-order script + per-shot QA/defects manifest +
  downloaded clips + a static HTML viewer. Built for exactly this campaign
  (its own docstring says so).
- **Environment**: `backend/.env` already has live, non-placeholder
  `DASHSCOPE_API_KEY` / `OPENAI_API_KEY` / `MINIMAX_API_KEY`;
  `KINORA_LIVE_VIDEO=true`; `VIDEO_BACKEND=minimax`; `REASONING_PROVIDER=openai`.

Dormant, and the central engineering opportunity of this plan:

- **`backend/app/render/event_director.py` + `backend/app/render/continuity_qa.py`** —
  a fully built, fully tested multi-shot "event" renderer (clusters beats into
  3-6 shots or single ≤15s packed segments, renders them concurrently, stitches
  into one continuous film with explicit last-frame continuity hand-offs) plus
  a deterministic seam-continuity scorer (geometry/aspect match, mode-chaining,
  hand-off presence, 180°-rule violations) and a narrative-persistence drift
  detector (unmotivated wardrobe/setting/lighting/time-of-day changes across
  *adjacent* chained shots). **Zero callers outside its own tests.** Also the
  entire substance of the previously-recorded "single-clip 15s overhaul"
  (`plan_segment_script` IS that overhaul's planner) — wiring it live retires
  both dormant efforts in one investment.

Explicitly irrelevant, left untouched: the "60-subsystem platform expansion"
(commits `9e3db09`/`c29e1d6`) — real, tested code, but billing/GraphQL/plugin/
compliance/moderation/search surface with no bearing on story accuracy or video
sync. The native SwiftUI shell (`apps/desktop-native`) — not backend-wired, so
there is nothing about story accuracy to test there. A separate, unrelated
12-agent "overnight fleet" (`agent-prompts/`, `coordination/`) ran once
(2026-06-26), completed, and was superseded by later work directly on `main`;
it is dormant history, not part of this plan.

## 3. Architecture change: live event-level continuity + repair loop

**Alternative considered and rejected:** leave `event_director`/`continuity_qa`
dormant and only run their scoring functions read-only, after the fact, inside
`review_export.py`. Rejected — this caps the payoff at *detection*, not
*correction*. The ultimate version makes the agents fix what they find, live.

- New setting `render_granularity: Literal["shot", "event"] = "shot"` (default
  unchanged — existing behavior is the fallback). The 10-book campaign runs
  with it set to `"event"`.
- When `"event"`, the Scheduler's promotion step groups a scene's ready beats
  into packed segments (`app.render.segment_packer.pack_segments`, already
  used by `plan_segment_script`) instead of promoting one bare shot at a time,
  and hands the group to `EventDirector.render_event` instead of
  `RenderPipeline.render_shot`.
- The live Wan/MiniMax/ModelScope/local renderer is injected as the
  `EventShotRenderer` (replacing the default `KenBurnsEventRenderer` only when
  `KINORA_LIVE_VIDEO` is on), so the same off-gate zero-spend proof path
  `event_director.py` already documents continues to work unchanged when live
  video is off.
- **The repair loop is real, not advisory:** today `EventDirector.render_event`
  computes an `EventContinuityReport` and only logs it. This plan makes the
  router (`route_event_continuity`) actually act: `INSERT_SUPPLEMENTAL`
  triggers `propose_supplemental_shot` + a re-render + re-stitch;
  `REGEN_CONTINUATION` re-renders the offending shot at corrected geometry;
  `DEGRADE` falls to the Ken-Burns hold for that event (never ships a
  known-bad seam). `detect_persistence_drift` is wired as a second,
  narrative-level gate alongside the geometric seam score — a
  wardrobe/lighting/setting flicker with no motivated change routes through
  the same repair path.
- **Client impact, proven before use:** the client's unit of playable delivery
  becomes one merged event clip (`clip_key = keys.clip(book_id, event_id)`)
  instead of one shot clip. The merged `SceneSyncMap` still carries per-shot
  segment boundaries and cumulative timecodes (`merge_sync_segments` already
  produces this), so `useScrollFilm.ts`'s word-position → segment → seek-time
  resolution changes from "resolve to a clip URL" to "resolve to a clip URL +
  an in-clip timecode offset." Proven with real Playwright-driven verification
  (scroll through a seeded book, confirm seek lands on the correct in-clip
  offset across a multi-shot merged event) *before* any of the 10 books run
  against it.
- The existing single-shot Critic/Continuity/Showrunner gate runs exactly as
  it does today on each shot inside the event before the event-level seam
  check runs on top. Two layers, not a replacement — nothing currently working
  can regress.

## 4. NEW: whole-book long-range continuity audit

Section 3 catches drift between *adjacent* shots inside one event. It
structurally cannot catch a character's eye color changing between chapter 2
and chapter 15 — shots that are never adjacent, never compared. Nothing in
this codebase catches that today, dormant or otherwise. This is new
capability, not a rewire, and it is the single highest-ambition piece of this
plan.

- New module `backend/app/render/book_continuity_audit.py` (house style: pure
  functions over already-known data, no ffmpeg, exhaustively unit-testable),
  run once per book after all of its events have rendered — a genuine new
  "Continuity Auditor" pass, conceptually the seventh crew role even though it
  is implemented as a function, not a service.
- For each persistence dimension already defined in `continuity_qa.py`
  (wardrobe, setting, lighting, time_of_day — extended here to character
  appearance/props via the same canon `active_states` shape §8), walk every
  accepted shot for the book **in reading order** (not just adjacent pairs)
  and track the last established value per entity. A change is flagged unless:
  (a) it is tied to a beat whose summary/hand-off narratively motivates it
  (generalizing `continuity_qa._change_motivated` from one seam to the whole
  book), or (b) it is preceded by a fresh establishing shot (a new
  chapter/scene legitimately resets context — not every wardrobe change 40
  pages later is an error, most are just the story moving on).
- Cross-checks the canon's authoritative locked state (`canon.query`, §8.3) at
  each shot's point in the story against what that shot actually depicts —
  catching cases where a shot contradicts a canon fact that was locked
  *after* the shot's original render, which the per-shot Critic (checked only
  at render time) cannot retroactively see.
- Output: a `BookContinuityReport` with entries anchored to specific
  (possibly far-apart) shot pairs, a confidence score per finding, and a
  recommended action:
  - **High confidence, single deviating shot, cheap to fix** → auto-regenerate
    that one shot via the existing director-edit regen pathway
    (`POST /sessions/{id}/comment`-equivalent internal call) — canon is
    authoritative, so the shot that contradicts an established canon fact is
    the one that's wrong, not the earlier one that set the fact.
  - **Lower confidence or ambiguous (could be legitimate story development)**
    → logged to the book's `DEFECTS.md` for human review, never auto-regenerated —
    long-range "drift" is sometimes just the plot; the bar for an automatic
    rewrite of something rendered many scenes ago is deliberately high.
- This closes the loop the event-level system opens: shot-level accuracy
  (existing Critic) → adjacent-shot seam accuracy (§3) → whole-book long-range
  accuracy (this section). Three layers, each catching what the layer below
  structurally cannot.

## 5. Fixing root causes, not just symptoms

When the same kind of defect recurs across multiple shots or multiple books
during this campaign, the response is not "regenerate it again" — it is
"the responsible agent's prompt, contract, or threshold is wrong, fix that."
Concretely: after the book-1 pilot, and again after every 2-3 subsequent
books, review the accumulated `DEFECTS.md` entries for repeated categories
(e.g., the Critic's CCS threshold consistently passes shots a human would
reject; the Adapter's shot-decomposition heuristic consistently
under-segments action scenes; a specific art-direction style token
consistently drifts). A repeated pattern gets a root-cause fix in the
responsible agent (`backend/app/agents/*`) — a tightened threshold, a
corrected heuristic, a clarified contract — with its own regression test, not
just another one-off shot regeneration. This is what makes the crew actually
improve over the course of the campaign instead of just cleaning up after
itself book by book.

## 6. Multi-provider video strategy

Goal: maximize genuine generated-video coverage per book while treating the
$25.44 MiniMax balance as scarce — this guardrail is unchanged by "ultimate."

| Provider | Role | Cost | Notes |
|---|---|---|---|
| MiniMax (`minimax.py`, live) | Gap-filler for shots the free tiers can't cover well | Paid, capped at **$15 total** for this campaign (code default guard stays $30; this is a tighter self-imposed working ceiling) | Already wired, already tested |
| ModelScope (new) | Primary real-video workhorse | Free, recurring daily quota | Same Wan model family as the existing DashScope integration — new `video_backend="modelscope"` branch reusing the exact `VideoBackend` Protocol / submit→poll→retrieve→download→`record_usage` shape `minimax.py` established. The exact video-generation endpoint contract is **not publicly documented** (verified 2026-07-04: only an analogous async image-generation pattern is confirmed) — implementation starts with a real empirical probe against the live API, not assumed schema. Needs a user-supplied `MODELSCOPE_API_TOKEN` (free signup at modelscope.cn) — build proceeds without it and activates the moment the token exists. |
| Local self-hosted — **stretch, not core** | Tertiary free fallback, if time allows after the 10 books are solid | Free (compute only), but real build cost | **Correction (verified 2026-07-04): no local Wan2.2/MPS provider exists in this repo today** — the project memory describing one was stale/from an unmerged branch. Building this means standing up a real local inference server from scratch, not hardening something that exists. Given the core goal is proving story-accuracy/video-sync across 10 books, not building a new inference runtime, this is demoted to an explicit stretch item, attempted only after Sections 3-9's core scope is solid. |
| Hugging Face ZeroGPU Spaces | Opportunistic spot-check only | Free, 5 min/day | Too fragile (depends on a specific community Space staying up, historical quota-enforcement bugs) for batch use. Used only for one-off verification, never load-bearing. |
| DashScope's own video quota | Not counted on | Unknown/likely none | Already an active, billing DashScope account; any one-time trial credit was almost certainly consumed at account creation, if it ever existed for video specifically (it's a text-token grant, not a video-seconds grant, per verification). If the user checks the Alibaba Cloud console and finds a leftover balance, that's a bonus, not a dependency. |
| fal.ai / Replicate | Rejected for this campaign | One-time signup credit only | Not recurring; would just convert to "another $10-20 balance to protect," not a real free tier. Documented fallback if all else is exhausted mid-campaign. |

**Selection mechanism (corrected from the original draft): reuse the existing
`VideoRouter`/`RouterPolicy`/`BackendTier` machinery** (`backend/app/providers/video_router.py`)
instead of hand-rolling new fallback logic — it is real, tested, and already
provider-agnostic (`Sequence[VideoBackend]`), but is currently only assembled
across Wan model-id variants on one DashScope client via the existing
`create_video_router()` helper, never across heterogeneous *providers*. This
plan's actual new work is assembling `VideoRouter([modelscope, minimax, ...],
policy=RouterPolicy(mode=COST_AWARE), tiers={...})` and wiring that as
`create_providers()`'s `video=` value, ordering free providers ahead of
MiniMax by tier. Every provider choice and its outcome (success/fallback/cost)
is logged per shot for the campaign report.

## 7. The 10 books

Full text, zero truncation — including for ingestion. `seed_library_100.py`'s
`PAGES_TO_RASTERISE = 8` demo-shelf shortcut is not used for any of these 10;
ingestion follows `seed_public_domain.py`'s full-EPUB pattern (generalized from
its current hardcoded 5-title list to the 10 below), sourced from the 130-title
`assets/books/catalog.json`.

| # | Title | Author | Why this book stresses something different |
|---|---|---|---|
| 1 | Alice's Adventures in Wonderland | Lewis Carroll | Whimsical, episodic — rapid scene/character changes |
| 2 | Pride and Prejudice | Jane Austen | Dialogue-dense, almost no action — sync during "quiet" prose |
| 3 | Moby Dick | Herman Melville | Long, descriptive — full-length scale stress test |
| 4 | Frankenstein | Mary Shelley | Gothic, moody first-person, strong visual imagery |
| 5 | Crime and Punishment | Fyodor Dostoevsky | Long, interior/psychological — little to visually anchor to |
| 6 | The Count of Monte Cristo | Alexandre Dumas | Sprawling plot across years/locations — hardest canon-consistency test, longest book, primary target for Section 4's long-range audit |
| 7 | Dracula | Bram Stoker | Epistolary, multiple narrators — POV/identity consistency |
| 8 | The Hound of the Baskervilles | Arthur Conan Doyle | Atmospheric mystery — sustained mood/lighting continuity |
| 9 | The Wonderful Wizard of Oz | L. Frank Baum | Literally color-coded regions — natural palette-continuity test |
| 10 | Treasure Island | Robert Louis Stevenson | High-action, physical — motion/render-mode decisions under action |

Books 1-6 already have DB rows (currently 8-page demo stubs sharing these
titles/covers) — re-ingest fully rather than starting fresh, so cover/catalog
metadata carries over. Books 7-10 are new ingests.

**Stretch (explicit, after all 10 are rock-solid, not a substitute for them):**
extend the same pipeline to more of the 130-title catalog. Not a core
commitment — the 10 above are.

## 8. Full edge-case + concurrency matrix

Per book, not just a happy-path scroll-through (drawn from kinora.md's own
risk table plus this design's own new surfaces):

1. **Fast-skim / seek-away mid-render** — jump far ahead while shots are
   in-flight; confirm cancellation + instant keyframe bridge + re-seed (§4.8).
2. **Idle-pause/resume** — stop scrolling 8s+; confirm speculation halts and
   resumes cleanly (§4.7).
3. **Backend-down fallback** — kill the API mid-session; confirm the bundled/
   Ken-Burns fallback plays with no broken/blank state.
4. **Budget-low degradation** — force the ledger near a cap; confirm graceful
   ladder degradation, not a hard stop (§12.1/§12.4).
5. **Director-edit repair loop** — trigger a real (or deliberately induced)
   defect through the region-comment → `POST /sessions/{id}/comment` path and
   confirm the regenerated shot actually fixes it.
6. **Cross-session preference persistence** (kinora.md §9.6) — a director
   edit's implied preference (pacing/palette/framing) made in one session must
   carry into a *new* session reading the same book later; verify it actually
   does, not just that it's recorded.
7. **Long-range continuity audit fires correctly** (Section 4) — for the
   longest books (Count of Monte Cristo especially), confirm the whole-book
   pass actually catches at least one real or seeded long-range contradiction
   and handles it per the confidence-based routing in Section 4.

Plus, at least once across the campaign (not per book): **a genuine concurrent
run** — two or more of the 10 books rendering through the queue at the same
time, proving the scheduler/budget ledger/worker are correct under real
parallel load rather than only ever exercised one book at a time.

Each of these is its own captured artifact (screenshot sequence or short
recording), not just a pass/fail note.

## 9. Artifacts & reporting

Per book, under `qa-runs/2026-07-04-10-book-campaign/<book-slug>/`:
- The `export-review` bundle: `script.md`, `manifest.json` (numeric QA/CCS
  scores, seam-repair actions taken, long-range-audit findings — not just
  pass/fail), `index.html`, `clips/`.
- Screenshots + short recordings for the open/scroll/close happy path AND
  every edge case in Section 8, mirroring the existing walkthrough pattern
  already in this repo (`coordination/artifacts/agent-12/`).
- A per-book `DEFECTS.md`, split into three tiers: seam-level (Section 3),
  long-range (Section 4), and pattern-level agent fixes (Section 5) — each
  with root cause, fix, and regression test added. No silent patches at any
  tier.

Cross-book, at the campaign root:
- `REPORT.md` — one thing to read first: per-book health (shot count, accept
  rate, regen count, repair actions taken, long-range findings, defects
  found/fixed, provider mix used, spend), plus the campaign-wide kinora.md
  §13 proof-metrics computed from real data: CCS distribution, regeneration
  rate, accepted-footage efficiency, a real buffer-occupancy sawtooth across
  an actual session, and a new metric this campaign introduces —
  long-range-contradiction catch rate.
- A cross-book `index.html` linking all 10 `index.html` viewers.

**Committed to the repo:** all of the above except raw clips. **Local-only,
gitignored:** the full per-shot MP4s in each book's `clips/` — kept on disk for
the user's own review, not pushed, so git history doesn't balloon with
gigabytes of video.

## 10. Testing & verification strategy

- New backend code (ModelScope provider, event-live-wiring, repair-loop
  actions, the book-continuity-audit module, local-provider hardening)
  follows TDD: failing test first, minimal implementation, `make lint` +
  targeted `pytest` green before moving on — same discipline `minimax.py`'s
  own tests already demonstrate (mocked HTTP via `httpx.MockTransport`, no
  live network in unit tests).
  - ModelScope provider tests mirror `test_providers_minimax.py`'s structure
    (gate, submit-body shape, poll mapping, success path, quota-exhaustion
    fallback).
  - Event-live-wiring tests cover: scheduler promotes an event not a shot when
    `render_granularity="event"`; a failed seam actually triggers the correct
    `SeamRepair` action; a persistence drift actually routes through repair;
    existing shot-granularity behavior is provably unchanged when the setting
    stays `"shot"` (regression guard).
  - Book-continuity-audit tests cover: a genuinely motivated long-range change
    is NOT flagged; an unmotivated one IS flagged with correct confidence
    routing; a canon fact locked after a shot's render correctly retroactively
    flags that shot.
- Client-side: the merged-clip seek-resolution change gets its own Vitest unit
  coverage (segment → in-clip offset math) *and* a real Playwright-driven
  verification pass against a seeded book before any of the 10 books run.
- `make test` and `pnpm --filter @kinora/desktop run typecheck && test && build`
  stay green at every major milestone (after the architecture change lands,
  after each provider is added, after the audit module lands), not only at
  the very end.
- The 10-book run itself is the acceptance test: "is the correct video showing
  at the correct point, and is the story accurate" is verified **by eye**
  through the review-export HTML and the edge-case recordings — that is the
  entire reason that tool and this artifact set exist.

## 11. Execution plan (phases)

1. **Foundation** — ModelScope provider + local-provider hardening + the new
   book-continuity-audit module (each independently testable/mergeable), full-
   ingest script generalized to the 10 titles, `render_granularity` setting
   added (default `"shot"`, inert).
2. **Live-wire the event/repair loop** behind `render_granularity="event"`,
   off by default. Client seek-resolution change + its Playwright
   verification. This is the highest-risk phase and lands, and is proven,
   before any book is run against it.
3. **Ingest all 10 books, full text**, zero truncation.
4. **Pilot: book 1 end-to-end** with `render_granularity="event"`, the full
   edge-case matrix (Section 8), and the long-range audit; scrutinized
   closely, fix whatever it reveals, apply Section 5's root-cause review
   before continuing.
5. **Books 2-10** — once the pilot is clean, these are independent of each
   other (different books, same proven pipeline) and get dispatched in
   parallel rather than strictly sequentially, each producing its own artifact
   set per Section 9. A Section 5 root-cause pattern review runs again after
   every 2-3 books.
6. **The one campaign-wide concurrency stress run** (Section 8) and the final
   `REPORT.md` roll-up.

Progress is checkpointed durably (an SDD-style ledger under
`.superpowers/sdd/progress.md`, matching this repo's existing convention) so
the campaign survives a context compaction without losing state.

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Event-level live-wiring regresses the currently-solid shot-granularity path | Default stays `"shot"`; campaign explicitly opts into `"event"`; existing shot-path tests must stay green (regression guard in Section 10) |
| Client merged-clip seek math is wrong | Proven with real Playwright verification *before* any book runs against it (Section 3, Section 10), not discovered mid-campaign |
| The long-range audit (§4) flags legitimate story development as "drift" | Motivated-change heuristic + fresh-establishing-shot exemption; auto-regen only at high confidence, everything else goes to human-reviewed `DEFECTS.md` |
| ModelScope's free quota is smaller/stricter for video specifically than the general 2,000-calls/day figure suggests | Verify empirically with a single test call before relying on volume; local self-host is the fallback if it's too thin |
| $15 MiniMax ceiling is reached before all gaps are filled | Provider order (Section 6) tries free tiers first; if $15 is hit, remaining gaps render Ken-Burns rather than exceeding the ceiling — never silently overspend |
| 10 full novels (especially Count of Monte Cristo, Moby Dick) take very long to ingest/render even on the free path | Phase 5 parallelizes books 2-10 once the pipeline is proven in the book-1 pilot |
| Root-cause agent fixes (Section 5) introduce their own regressions | Each gets its own regression test before being considered done, same TDD discipline as everything else in Section 10 |
