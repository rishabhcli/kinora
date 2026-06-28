# Next-generation reading-room playback — DESIGN

**Domain owner:** `apps/desktop/src/reading/` (the Scroll Film Engine and everything that
paints / streams / instruments the vertical AI film).

**North star:** the reading room is one continuous film the reader scrubs by scrolling
(kinora.md §4.3, §5.2). It must stay buttery at 60fps, never show a black frame, work
fully on bundled Ken-Burns mp4s with `KINORA_LIVE_VIDEO` OFF, and degrade gracefully
(§12.4 ladder) under bandwidth / decode / GPU pressure — while a curious judge can watch
the machinery work (§12.5 observability) without a reader ever thinking about it.

This document is a **living roadmap**: each phase lists what is DONE (shipped + tested in
this worktree) vs. REMAINING.

---

## 0. Hard constraints (never violated)

- **The pure `timeline.ts` contract is frozen.** `buildTimeline / resolvePlayhead /
  computeFrame / segmentTime / classifyScroll / schedulerSignal / scrollVelocity /
  nextSegmentToPreload` keep their signatures. New playback subsystems consume `Timeline`
  / `Frame`; they never change the sync math.
- **No-black-frame guarantee.** The CSS opacity crossfade in `FilmPane.tsx` remains the
  source of truth for *what src is on screen*. Every new layer (GPU compositor, quality
  switch, scrub) is **additive on top** and refuses to paint until it has a real decoded
  frame; if it can't, the proven CSS film shows through. There is never a moment where a
  new subsystem is the only thing on screen.
- **`KINORA_LIVE_VIDEO` stays OFF.** Everything works on the bundled fallback films. Live
  clips are an input, not a requirement.
- **Stay in `apps/desktop/src/reading/`.** The component shell, library, and director studio
  belong to other agents. `apps/desktop/src/lib/api.ts` is consumed read-only; any change
  there is additive-only and recorded in §"Cross-domain contract changes" below.
- **Do not commit.** Work stays in the worktree.

---

## 1. Architecture

The reading room is layered. Each layer is independently testable; the lower (proven)
layers keep working if an upper (enhancement) layer bails.

```
useScrollFilm (rAF loop, DOM adapter)         ← unchanged hot path
        │ Frame (from pure timeline.ts)
        ▼
FilmPane.tsx  ── CSS opacity crossfade + warm <video> pool  ← no-black-frame source of truth
        │ (the two visible <video> layers + their currentTime)
        ▼
[ gl/ WebGLCompositor ]  ── OPTIONAL GPU pass: GPU crossfade + colour grade + grain
        │  mounted only when capabilities.decideCompositor() says yes
        ▼
<canvas> overlay (hidden the instant the GPU path isn't operational)

Side channels (pure cores, fed by thin DOM adapters):
  perf/      frame-budget / jank / decode-health instrumentation  (§12.5)
  streaming/ bandwidth estimate → adaptive quality ladder          (§12.4, §4.6)
  offline/   service-worker cache protocol for clips + page text   (planned)
  scrub/     frame-accurate scrubbing model                        (planned)
  gesture/   touch + picture-in-picture                            (planned)
```

### Why a separate GPU compositor instead of replacing the CSS crossfade

Electron 33 / Chromium ~130 composites `<video>` opacity transitions on the GPU already,
and that path has the no-black-frame guarantee baked in and battle-tested. Throwing it out
for WebGL would risk the one invariant we can't lose. So the GPU compositor is a *second*
surface layered over it: it samples the same two `<video>` elements as textures and adds
the things CSS can't do (true GPU crossfade curves, ASC-CDL colour grade, vignette,
animated film grain, future shader transitions). When WebGL is unavailable, the user asked
for reduced motion, the program won't compile, or the renderer is already janking, we
simply don't mount the canvas — and the CSS film is exactly what ships today.

---

## 2. Phased roadmap

### Phase 1 — GPU compositor core + instrumentation + adaptive quality — **DONE**

Pure, DOM-free, fully unit-tested cores plus the one runtime GL wrapper.

| Module | What it is | Tests |
|---|---|---|
| `gl/shaders.ts` | WebGL2 GLSL (fullscreen-triangle vert; crossfade→grade→grain frag) + uniform/attribute inventory | (compiled on-GPU via probe) |
| `gl/grade.ts` | `FilmGrade` model, preset library, CPU reference of the shader grade, `lerpGrade` | `gl/grade.test.ts` (node) |
| `gl/capabilities.ts` | `decideCompositor()` policy + `probeGl()` runtime probe (injectable gl) | `gl/capabilities.test.ts` (node) |
| `gl/program.ts` | compile/link a WebGL2 program, resolve uniforms; errors returned not thrown | (via compositor test) |
| `gl/webglCompositor.ts` | the GPU compositor class: context, 2 video textures, fullscreen draw, context-loss handling, dispose | `gl/webglCompositor.test.ts` (vitest, mock gl) |
| `gl/transitions.ts` | named GPU scene transitions (dissolve/soft/bloom/desat-dip) → {mix, grade} over progress; no dip-to-black | `gl/transitions.test.ts` (vitest) |
| `perf/frameStats.ts` | rolling-window frame-budget / jank / dropped-frame / p95 stats, O(1) push | `perf/frameStats.test.ts` (node) |
| `perf/decodeStats.ts` | per-interval decode-health deltas from cumulative `getVideoPlaybackQuality()` readings | `perf/decodeStats.test.ts` (node) |
| `perf/observability.ts` | §12.5 fusion of frame+decode+quality+buffer into one panel snapshot + coarse health grade | `perf/observability.test.ts` (node) |
| `streaming/bandwidth.ts` | asymmetric-EWMA throughput estimator (kbps), conservative on the way up | `streaming/bandwidth.test.ts` (node) |
| `streaming/qualityLadder.ts` | the §12.4 client ladder + ABR controller (bandwidth × buffer × decode × device → rung) with upgrade hysteresis | `streaming/qualityLadder.test.ts` (node) |
| `streaming/abrSim.ts` | pure trace-replay harness scoring the controller (switches, dwell, starvation) for offline tuning | `streaming/abrSim.test.ts` (vitest) |

Guarantees encoded in Phase 1:
- the compositor's `render()` returns `false` (draws nothing) until layer A has a decoded
  frame, so the GPU path can never produce the first black frame;
- the quality ladder's bottom rung (audio + karaoke text, §12.4) is always selectable
  (`minKbps 0`), so adaptation never blanks the pane;
- decode distress and rAF jank force a hard floor on the rung regardless of bandwidth.

### Phase 2 — wire the cores into the engine (DOM adapters) — **DONE (hooks + overlay)**

- `perf/usePerfMonitor.ts` — rAF-driven adapter that feeds `FrameStats` real timestamps and
  polls `getVideoPlaybackQuality()` into `DecodeStats` at a low cadence; exposes a snapshot
  via `useSyncExternalStore`. **DONE.**
- `gl/GpuFilmOverlay.tsx` — a `<canvas>` overlay mounted inside the FilmPane stack that
  binds the two visible `<video>` elements as `FrameSource`s, runs the compositor, and
  hides itself the instant the compositor isn't operational. **DONE** (`GpuFilmOverlay.test.tsx`).
  Adoption inside `FilmPane`/`ScrollFilmEngine` render trees is deferred to Phase 7 (those
  files are large and shared with the no-black-frame guarantee; the overlay is self-contained
  and adoptable behind a prop without touching the sync hot path).
- `streaming/useAdaptiveQuality.ts` — adapter that folds the SSE `buffer_state`, the
  bandwidth estimator (fed by `ClipCache` byte/time samples), and `DecodeStats` into a
  `QualityController.update()` each decision tick; exposes the chosen rung. **DONE**
  (`useAdaptiveQuality.test.tsx`).
- `streaming/instrumentedFetch.ts` — a `fetch` wrapper that reports each clip download as a
  {bytes, durationMs} throughput sample to the estimator (tees the body, never alters the
  caller's Response). **DONE** (`instrumentedFetch.test.ts`).
- `playback/index.ts` — the single public barrel for all playback subsystems. **DONE.**

### Phase 3 — frame-accurate scrubbing — **DONE (core + RVFC adapter)**

- `scrub/frameClock.ts` — map a scrub position to an exact frame index given fps + clip
  duration; quantise `currentTime` to frame centres so a paused scrub lands on a stable
  frame (no inter-frame shimmer). **DONE.**
- `scrub/requestVideoFrameCallback.ts` — wrap `HTMLVideoElement.requestVideoFrameCallback`
  (Chromium) with a rAF fallback, exposing presented-frame metadata for sub-frame seek
  confirmation + decode timing. **DONE.**
- `scrub/seekPlan.ts` — bridges the timeline's target time to FilmPane's seek: continuous
  (epsilon-gated) seeks while scrubbing, frame-quantised + same-frame-deduped seeks while
  settling, so a paused scroll position always rests on one repeatable frame. **DONE**
  (`scrub/seekPlan.test.ts`, vitest).
- REMAINING: a thumbnail/sprite filmstrip (decode a low-res strip for a scrub preview),
  and adopting `planSeek` inside FilmPane's seek (currently 30Hz-throttled raw seeks).

### Phase 4 — adaptive streaming end-to-end — **DONE (client) / REMAINING (backend hookup)**

- DONE: ladder + controller + bandwidth estimator + `useAdaptiveQuality` adapter +
  byte-accounting `instrumentedFetch` (ClipCache feeds real samples) + `abrSim` for offline
  tuning/validation.
- REMAINING (needs additive backend contract): variant URL resolution (`clip_variants` on
  `ShotResponse`, see cross-domain notes); pre-fetch the *next* shot at the *selected* rung;
  emit a `quality_intent` upstream so the scheduler can bias render fidelity.

### Phase 5 — offline service-worker cache — **DONE (logic + protocol + manifest + hook) / REMAINING (deployed worker file)**

- DONE: `offline/swProtocol.ts` — typed page⇄worker message protocol + cache-key scheme +
  per-asset strategy (clip = cache-first, page = stale-while-revalidate). `offline/manifest.ts`
  — prioritised, budget-bounded precache manifest + LRU eviction plan. `offline/readingSwCore.ts`
  — the full worker LOGIC (fetch routing, precache w/ progress, evict, status) over an injected
  CacheStorage so it's unit-tested without a real worker. `offline/useOfflineCache.ts` — the
  page-side registration + message hook, best-effort (no-ops without `navigator.serviceWorker`).
- REMAINING: the thin deployed `public/reading-sw.js` entrypoint that imports `readingSwCore`
  and wires the real `self`/`caches`/`fetch` (lives in `public/`, outside this domain — a
  Phase-7 integration step); range-request handling for partial mp4 caching; a "downloaded for
  offline" UI affordance in the shell (components agent).

### Phase 6 — gesture / touch / picture-in-picture + a11y — **DONE (cores) / REMAINING (wiring)**

- DONE: `gesture/scrubGesture.ts` — pointer/touch → timeline-fraction delta model with EMA
  velocity + a decaying fling, pure + tested. `gesture/pictureInPicture.ts` — a PiP
  controller (enter/exit/toggle + capability + state) over the browser PiP API, stub-tested.
- DONE (a11y): `describedVideo/describedVideo.ts` — the audio-description track model
  (contiguous cues, active-cue resolution, change-only announcement) feeding `a11y/announce`/
  `a11y/tts`.
- REMAINING: bind the gesture model to the scroll container's pointer events; expose
  full-keyboard scrub/frame-step/PiP commands (coordinate with `a11y/keyboard.ts`, additive).

### Phase 7 — render-tree integration — **REMAINING**

Adopt the overlay + perf + adaptive hooks inside `FilmPane`/`ScrollFilmEngine` behind a
default-off prop, prove the no-black-frame guarantee with a jsdom integration test, then
flip the default. Kept last so the proven hot path is touched only once, deliberately.

---

## 3. Testing strategy

Two runners, by design (matching the existing repo split):

- **`node:test`** (run by `src/test/run-node-tests.mjs`, Node strip-types, no jsdom) — for
  pure, DOM-free modules that import *no sibling source files* with extensionless
  specifiers. Fast, zero framework. Caveat: Node strip-only mode rejects TS *parameter
  properties* (`constructor(private x)`) and *extensionless relative imports* — pure cores
  avoid both (explicit field assignment; tests import the `.ts` directly).
- **vitest (jsdom)** — for tests that touch the DOM or import sibling source modules with
  extensionless specifiers (e.g. `webglCompositor.test.ts` → `./shaders`). Vitest resolves
  those the way the Vite build does.

`vitest.config.ts` excludes the node-test files (see §4). The full gate is
`pnpm --filter @kinora/desktop run typecheck && run test` (test = vitest + the node runner).

**Current state of this worktree (all green):** `tsc --noEmit` clean; `vite build` clean;
vitest **21 files / 147 tests**; node runner **24 files**. The pre-existing suite is
untouched and still passes; every number above is additive over the baseline (which was
13 vitest files / 106 tests + 9 node files).

---

## 4. Cross-domain contract changes (additive only)

Recorded here per the worktree rules. Nothing existing was modified or removed.

1. **`apps/desktop/vitest.config.ts`** — *additive excludes only*. Added the new reading
   sub-suite `node:test` files to the vitest `exclude` array so they run on the node runner
   instead of jsdom (same mechanism already used for `crossfade/fallback/machine/warmupModel`).
   No behavior change to existing tests. The exact additions:
   ```
   src/reading/perf/{frameStats,decodeStats,observability}.test.ts
   src/reading/streaming/{bandwidth,qualityLadder,instrumentedFetch}.test.ts
   src/reading/gl/{grade,capabilities}.test.ts
   src/reading/scrub/{frameClock,requestVideoFrameCallback}.test.ts
   src/reading/offline/{swProtocol,manifest}.test.ts
   src/reading/gesture/*.test.ts
   src/reading/describedVideo/*.test.ts
   ```
   (Tests that import sibling *source* modules — `webglCompositor`, `transitions`, `abrSim`,
   `seekPlan`, `readingSwCore`, the hooks — stay on vitest and are NOT excluded.)

2. **`apps/desktop/src/lib/api.ts`** — *no change yet.* Planned additive fields for Phase 4
   (not applied): an optional `clip_variants?: { rung: string; url: string; height: number }[]`
   on `ShotResponse`, and an optional `quality_intent(sessionId, rung)` POST. These are
   forward-looking; the current ladder works without them (it annotates the desired rung and
   resolves a single URL). When applied they will be strictly optional so existing callers
   and the backend contract are unaffected.

All new code lives under
`apps/desktop/src/reading/{gl,perf,streaming,scrub,offline,gesture,describedVideo,playback}/`.
The single public surface is `apps/desktop/src/reading/playback/index.ts` (a barrel).
`apps/desktop/src/lib/api.ts` was **not** modified.
