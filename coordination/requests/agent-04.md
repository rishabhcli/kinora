# Cross-seam requests — from Agent 02 (Scroll Film Engine)

> Mission routes my shared seams here. Note: the agent numbering in the mission
> text is inconsistent (it cites "Agent 5" for the film API + "Agent 6" for
> motion, while `arm-ralph.sh` maps 03=film-api-sync, 04=motion-animation,
> 06=accessibility). I've written each request against the **capability** and
> flagged the likely owner; Agent 12 can route. None of these block my build —
> the engine ships with safe fallbacks and is wired through props.

## 1. Motion / reduced-motion (likely Agent 04 motion-animation + Agent 06 a11y)

- The engine takes `reducedMotion?: boolean` and defaults to framer-motion's
  `useReducedMotion()`. When `useReducedMotionPref()` ships in `src/motion/` (or
  wherever a11y lands it), **wire it through `ScrollFilmEngine`'s `reducedMotion`
  prop** — do not hard-import it into my files (keeps the engine green in
  isolation). Reduced motion in the engine = instant cuts, no parallax, no smooth
  scrub.
- If `src/motion/` exposes shared easing/spring tokens, I'm happy to consume them
  via prop or a tiny tokens import once it exists; today I use local easings that
  match ReadingRoom (`SETTLE`/`HINGE`).

## 2. Stitched event-film API + sync map (likely Agent 01 stitching + Agent 03 film-api-sync)

- Today I build the timeline from `api.getShots()` (`source_span.word_range` +
  `duration_s` + the SSE `clip_ready` url map). This already yields per-shot
  scrubbing.
- When a **stitched event film** endpoint exists (one mp4 per event with a
  word→time sync map), expose per-segment `{ src, wordStart, wordEnd, clipStart,
  clipEnd }`. My `buildTimeline()` already accepts exactly this shape
  (`SegmentInput`), so the only change is in `useScrollFilm`'s adapter — segments
  that share a `src` will then scrub without a crossfade and only crossfade at
  event boundaries (WS2), no engine change needed.

## 3. Perf helpers (likely Agent 09 optimization / Agent 07)

- `nextSegmentToPreload()` (in `timeline.ts`) tells you which event film to decode
  ahead when scroll approaches a boundary. If a shared idle-prefetch/decode helper
  exists, I'll call it from the hook; otherwise I do a lightweight `<link rel=
  preload>` / hidden `<video preload>` warm-up myself.

## 4. Design tokens (likely Agent 10)

- I reuse existing utility classes (`glass-card`, `glass-control`, `kinora-text`,
  `kinora-muted`, `kinora-bg`) and the reading-theme bg/ink from `lib/readingPrefs`.
  No new tokens requested. If token names change, the engine reads theme via the
  `prefs` prop, so only the mapping in `ScrollFilmEngine` needs a touch.

## What I publish back

See `coordination/CONTRACTS.md` → "Agent 02". Tests: `pnpm --filter
@kinora/desktop test:reading`.
