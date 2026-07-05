# Alice's Adventures in Wonderland — Defects (Task 13 pilot pass)

Book id: `04627bdd8ae54e41a03462760e43d3b1`. Driven live via the project's bundled
Playwright/chromium against `:5173`, real backend, real session
(`sess_7c219fe9709e47a3`), real MiniMax video generation.

## Pattern-level defects (fixed)

### 1. Project Gutenberg boilerplate shot-planned as narrative — CONFIRMED, FIXED

**Tier:** pattern-level (affects every campaign book; all 10 are Gutenberg releases).

Beats 0-10 of Alice's real, already-ingested data were verified (via direct
Postgres query) to be non-narrative Gutenberg boilerplate treated as story:

- beat 0: "The eBook opens with a Project Gutenberg notice for Alice's Adventures in Wonderland."
- beat 6: "The contents continue with chapters introducing the Rabbit, Bill, and a Caterpillar." (a table-of-contents *entry*, not the chapter)
- beat 8: "Chapter X is introduced with the title 'The Lobster Quadrille.'" (also a ToC entry)

The same pattern repeats at the end of the book (donation solicitation,
Project Gutenberg's own history) for roughly 80 more beats out of 809 total.
Each of these would be shot-planned and — once rendered — cost real video
spend depicting a copyright notice or a table of contents as if it were a
scene.

**Root cause:** `plan_and_persist` (`backend/app/ingest/shot_plan.py`) had no
concept of front/back matter; every page with `num_words > 0` was handed to
the Adapter unconditionally.

**Fix:** `gutenberg_content_page_range()` locates Gutenberg's standard
`*** START/END OF THE PROJECT GUTENBERG EBOOK ***` markers and bounds
scene/beat planning to the pages between them (commit `7e3f075`). Regression
tests cover: markers present, markers absent (full range preserved — must
never narrow a book it can't confidently bound), and a missing end marker.

**Not yet re-verified against Alice specifically**: applying this to Alice
requires a full reingest (the `plan_and_persist` step is not independently
re-runnable without redoing extraction/analysis), and Alice's existing canon
is genuinely good (12 locked principals from before the DashScope VL quota
was exhausted — see below). Re-ingesting her now would destroy that canon
for no benefit, since VL is still down. **Action needed before this book is
truly "campaign clean": reingest Alice once DashScope VL is confirmed
working.** Correcting my own earlier expectation here (see the P&P scope-limitation
note below, found afterward): beat 0 will open cleanly on the Gutenberg
marker's own page, but per the original beat mapping her page 4 ("The book's
title and author are presented as an opening title card") sits *after* the
START marker (page 3) and *before* the real story (page 5) — the same
publisher-front-matter gap as P&P, just one page instead of seventeen. Not
"perfectly clean" after the fix, just far better than before it.

**Confirmed scope limitation (found verifying the fix against Pride and
Prejudice's real, re-extracted pages — precise, not speculative)**: the fix
correctly excludes everything *outside* Gutenberg's own START/END markers
(verified: P&P's real page 2 ends with the literal marker text, page 1 is
correctly dropped). But this specific P&P edition — an illustrated edition
with a scholarly preface by George Saintsbury — has ~17 *more* pages
(3-18: preface, "List of Illustrations" with page numbers, a second
title/illustration page) positioned *between* the Gutenberg START marker and
the real opening line ("This was invitation enough. 'Why, my dear, you must
know...'", confirmed on page 19). None of that is Gutenberg's own
boilerplate — it's this edition's publisher-added front matter, which has no
universal, standardized delimiter the way Gutenberg's markers do. The fix as
shipped does exactly what its own docstring and commit message claim
(bounds by Gutenberg's markers); it does not, and was never designed to,
detect edition-specific editorial front matter. Deliberately NOT attempting
a heuristic "detect the real chapter 1 heading" fix under time pressure —
chapter-heading patterns vary too much across editions (numbered/unnumbered,
"Chapter"/"Part"/"Book", roman/arabic) to safely guess from one book's shape
without risking a false-positive that swallows a real opening chapter on a
*different* book. Flagging for the Task 13 Step 6 / Task 14 Step 2 root-cause
review: if this recurs on illustrated/annotated editions of other campaign
books, it's worth a dedicated, carefully-tested pass — not a rushed regex.

### 2. Typographic ligatures unnormalized in extracted narration text — CONFIRMED, FIXED

**Tier:** pattern-level (affects every campaign book).

Found while reading Alice's real, already-rendered shots by eye
(`export-review` output): narration text showed "Alice's ﬁrst thought",
"hurried oﬀ to the garden door" — PDF text-layer ligature glyphs (U+FB01
"ﬁ", U+FB00 "ﬀ", etc.) left unnormalized. Cosmetic (doesn't change meaning)
but real "Apple-level quality" polish, and — since narration is shown as
on-screen text/captions — could render as a missing-glyph box in a font
without these ligatures.

**Fix:** NFKC-normalize each word at extraction (`pdf_extract.py`'s
`_extract_page_words`), commit `776fd78`. Verified directly:
`unicodedata.normalize("NFKC", "ﬁrst") == "first"`, `"oﬀ" == "off"`. One
DB-free regression test (feeds a raw PyMuPDF word row directly, since
base-14 test fonts can't reproduce real ligature glyph rendering) + the 2
existing DB-backed tests all pass.

**Not yet re-verified against Alice specifically** — same reasoning as
defect #1: applying it requires a full reingest, which would hit the VL
outage and destroy her currently-good canon. Reingest Alice once DashScope
VL is confirmed working, alongside the boilerplate-range check.

## External-dependency blockers (not code defects — do not weaken the gates that expose them)

### 3. DashScope `qwen-vl-max` + `qwen3-tts-flash` free-tier quota exhausted

Confirmed via direct, repeated, zero-cost API calls (`AllocationQuota.FreeTierOnly`,
403) throughout this session. Blocks: (a) page-analysis for any new/re-ingested
book, (b) the Critic's `_vision()` timeline/motion-artifact check, (c) narration
TTS. `qwen-image-plus` (keyframes) and embeddings are unaffected — confirmed
working with real successful calls. Requires the account holder to add
payment information or disable "Free Quota Only" in the Model Studio console;
no code-level workaround exists (confirmed: no public OpenAPI action for this
toggle, and the console requires a login this environment doesn't have).

### 4. Real video generation succeeds but still ships as Ken-Burns — direct consequence of #3, not a separate bug

Live-verified end to end for shot `04627bdd8ae54e41a03462760e43d3b1_beat_0043_shot_00`:

1. `provider.call_ok model=MiniMax-Hailuo-2.3-Fast op=video video_seconds=6.0` — a
   real 6-second clip was genuinely generated and downloaded.
2. The Critic's QA pass then failed: `qa.unavailable error='...AllocationQuota.FreeTierOnly...' model=qwen-vl-max`.
3. Repair was attempted, also failed (TTS same cause), leaving `retries_exhausted`.
4. `degrade.ken_burns` / `degrade.shipped reason=critic_AuthenticationError` — the
   real MiniMax clip was discarded and Ken-Burns shipped instead.

This is `backend/app/render/pipeline.py`'s deliberate, documented safety gate
(`"an unverified clip must not ship as ACCEPTED"`, matching kinora.md
§7.2/§9.5's "never silently shipped" invariant) — correctly refusing to ship
a clip whose identity/style/timeline was never actually checked, not a bug in
that gate. **The fix is #3 (the DashScope account), not this gate.** Do not
weaken `_vision()`'s error handling to force real video through on a QA
provider outage — that would mean shipping content that was never verified
against canon, which is a worse outcome than a correctly-labeled Ken-Burns
degrade.

**Financial protection action taken**: the render queue kept draining —
104 shots queued in the committed lane, each generating a real, paid MiniMax
clip before discarding it for Ken-Burns (guaranteed outcome while DashScope
stays down). Checked `kinora:minimax:usd_spent` directly: **$11.21 of the
$15 MiniMax cap already spent** — continuing would very likely have blown
through the cap for zero usable output. Stopped `kinora-render-worker-1`
(a separate container from `kinora-api-1`, where P&P's reingest runs — zero
effect on it, confirmed). Verified spend is frozen at $11.21 post-stop.
**Must restart render-worker before any further real rendering work** —
left stopped deliberately until DashScope VL is confirmed fixed, so no
one accidentally resumes burning the remaining ~$3.79 of MiniMax budget on
guaranteed-degraded shots.

## Verified working end-to-end (not defects)

- Real session creation (`POST /api/sessions` → SSE `events` stream), real
  scroll→seek→intent flow, real scheduler `keyframe`→`committed` promotion,
  real MiniMax video generation and CDN download — all confirmed via direct
  backend log inspection during a live, realistic-paced scroll session.
- The scheduler correctly refuses to commit real video for content the
  reader has already scrolled past (observed: aggressive scrolling produced
  only keyframe jobs + `queue.cancel_distant`, never a wasted commit) — this
  is the fast-skim edge case (spec Section 8) behaving correctly, not a bug.
- **Idle-pause/resume** (spec Section 8): held a live session still for 45s —
  zero `seek`/`intent` calls fired during the idle window (no polling spam),
  and scrolling again immediately produced a fresh `seek`+`intent` pair. Pass.
- `export-review` CLI tooling (numeric QA scores, seam repair actions,
  long-range findings) verified structurally sound against existing "ready"
  demo data.
- **Long-range continuity audit** (spec Section 8 item 7): ran `export-review`
  against Alice's real, full 889-shot set (not just the 9 rendered ones —
  `review_export.py` runs `audit_book_continuity` over every shot with a
  resolvable beat) — `long_range_findings: []`, zero false positives on a
  real, legitimate book. Confirming the detector can also *catch* a real
  contradiction (not just stay quiet on a clean book) is explicitly scoped by
  the spec to the longest books, "Count of Monte Cristo especially" — correctly
  deferred to that book's own Task 14 pass, not forced here.
- **Backend-down fallback** (spec Section 8): the actual Docker stack
  restarted unprompted mid-session (a real, if uninvited, live instance of
  this exact scenario) — didn't have a browser session open at that exact
  moment to observe it visually, so verified the mechanism directly instead:
  `useFilmSession.ts` dispatches `FALLBACK` on every failure path (session
  create, SSE, "still preparing"), backed by `machine.test.ts`/
  `fallback.test.ts` (`node:test`, run via `run-node-tests.mjs` as part of
  `pnpm test` — NOT vitest, which deliberately excludes them; confirmed this
  is by design, not a coverage gap, by checking they're picked up by the
  node:test walker via their import statement). Ran the full node:test suite
  directly: 24 files, 0 failing.

## Full test-suite gate (Task 13 Step 7)

- Backend: 96 tests across every file touched or made relevant by this
  session's fixes (`test_ingest_shot_plan.py`, `test_ingest_pdf_extract.py`,
  `test_ingest_analyze.py`, `test_cli_integration.py`,
  `test_book_continuity_audit.py`, `test_eval_buffer_trace.py`,
  `test_api_prefs.py`/`test_prefs_signals.py`/`test_prefs_learning.py`) —
  all pass.
- Frontend: `pnpm --filter @kinora/desktop run typecheck` clean; `run test`
  — 105 vitest files / 833 tests, 24 node:test files, 15 electron test
  files, zero failures anywhere; `run build` — clean production build (611
  modules, only pre-existing i18n chunking notices, not errors).
- **Root cause found, verified against the live system, and FIXED (2026-07-05)**:
  the render queue's DLQ has 155 dead-lettered `keyframe` jobs from the
  earlier DashScope outage storm; every one sampled (20 checked directly via
  `redis-cli HGET ... error`) failed with the literal string "circuit
  breaker open; rejecting call without attempting" — confirming the
  documented symptom is real, not assumed. **Correction to the original
  claim above**: re-reading `ProviderClient._execute` line by line shows
  `AuthenticationError` (what a `403 AllocationQuota.FreeTierOnly` classifies
  to) is explicitly excluded from tripping the breaker at all (`except
  ProviderError: raise` — only `TransientProviderError` counts, per the
  code's own comment, "not a fault the breaker should count") — so the VL
  quota exhaustion could not have tripped it *directly*. The DLQ only stores
  each job's *last* error, not the original trigger, and the containers have
  since restarted (their logs from the actual trip are gone), so the exact
  triggering transient error is not recoverable after the fact. What *is*
  confirmed, either way: `backend/app/providers/base.py`'s `ProviderClient`
  built exactly **one** `CircuitBreaker` for the whole client, so once
  *anything* — any sufficiently sustained burst of a real transient error
  (5xx/timeout) on VL, image, TTS, or embeddings — tripped it, every other
  capability sharing that client got rejected too, regardless of its own
  health. That architectural gap is real regardless of which specific error
  first tripped it, and it's what actually explains "healthy `qwen-image-plus`
  calls got rejected" (confirmed via a real successful smoke call earlier).
  **Fixed**: `ProviderClient` now keys its breakers per capability
  (`_breaker_key`: `chat`/`vl`/`embedding` each independent;
  `image`/`image_edit` share one; every `video*` op shares one; `tts`/
  `tts_clone`/`asr` share one) instead of one for the whole client, so one
  capability's failure burst can no longer reject a different, healthy
  capability's calls. 3 new tests (existing 16 unaffected): one reproduces
  the exact live bug shape (a "vl" burst must not block a following "image"
  call) and is confirmed failing without the fix; one locks in that
  multi-step ops for the same backend (image/image_edit) correctly still
  share fate. The 155 already-dead jobs are inert (not retrying, not
  costing anything) — no action needed on them; this fix only changes
  behavior going forward.

## Verified via existing test coverage (live E2E deferred)

- **Cross-session director-preference persistence** (spec Section 8 item 6,
  kinora.md §9.6): a director edit's implied pacing/palette/framing
  preference must carry into a *new* session reading the same book later.
  The mechanism (`app/memory/prefs_service.py`'s `upsert_nudge`/`get_effective`,
  read by `pipeline.py` when planning a shot) is real and live-wired, backed
  by 40 passing tests across `test_api_prefs.py`/`test_prefs_signals.py`/
  `test_prefs_learning.py` (confirmed passing this session). A live E2E visual
  proof (make a director edit, open a fresh session, confirm a *new* shot's
  style actually reflects it) is deliberately deferred: every shot generated
  right now degrades to Ken-Burns regardless of preference (defect #4, same
  DashScope cause), which would make any visual comparison meaningless until
  that's resolved.
- **Director-edit repair loop** (spec Section 8 item 5): mechanism located
  (`DirectorStudio` overlay, launched via the library's "Director mode"
  toggle → `RegionCommentBar`/`ShotInspector` → `POST /sessions/{id}/comment`).
  Live E2E deferred for the same reason as above: any triggered regen would
  also degrade to Ken-Burns right now, so a live run would only prove the
  endpoint gets called, not that the repair fixes anything — not worth the
  session-setup cost until DashScope VL is back.
- (Unrelated observation, not this edge case) the "Enable AI Film generation"
  toggle (`useFilmSession.ts`'s `generateVideo`, a spend-control gate) resets
  to off on every new session/context — this is a *different* preference than
  #9.6's director-edit preferences and, per its own code comment ("gives
  users explicit control over generation spend"), appears intentionally
  session-scoped rather than a persistence gap. Not flagged as a defect.

## Known, deliberately out-of-scope for this pass

- A DB connection-pool exhaustion (`QueuePool limit of size 10 overflow 20`)
  was hit and resolved by restarting `kinora-api-1` (cleared ~10 stale idle
  sessions accumulated from a full day of live testing, not a leak in new
  code). Worth a follow-up if it recurs under real multi-user load — Task 15
  (concurrency stress) is the right place to characterize this properly.
