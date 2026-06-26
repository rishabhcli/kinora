# Agent 02 — progress (durable Ralph-loop tracker)

Branch `agent/02-scroll-film`, worktree `../kinora-a02`, base `overnight/integration`.

## Status legend: [ ] todo · [~] in progress · [x] done + verified

### Setup
- [x] Worktree + branch created off `overnight/integration` (created from `main`).
- [x] `pnpm install` in worktree; **baseline `typecheck` + `build` green** (clean ref point).
- [x] Test runner: Node `--experimental-strip-types` + `tiny-test.mjs` harness;
      `pnpm --filter @kinora/desktop test:reading`. Verified red→green + fail exits 1.

### WS-pure — timeline.ts (sync map math) — TDD
- [x] `buildTimeline` / `resolvePlayhead` / `focusWordFromFraction` / `segmentTime`
      / `classifyScroll` / `schedulerSignal` / `nextSegmentToPreload` — 18 tests green.

### WS1 — scroll → timeline scrubbing
- [x] `useScrollFilm.ts`: single rAF scroll→currentTime, EMA velocity scrub/play,
      scheduler signalling (postIntent/seek, throttled), idle self-stop + settle,
      dt-clamped velocity (post-idle flick fix, `scrollVelocity` unit-tested).
- [x] `ScrollFilmEngine.tsx`: scroll container + themed text column + film pane +
      rail + scrub indicator + parallax. GPU transforms only, imperative hot path.

### WS2 — cross-event handoff
- [x] `FilmPane.tsx`: ≤2 `<video>` layers, crossfade only on `src` change (so
      stitched-event segments scrub seamlessly; shot/event boundaries crossfade),
      instant hard-cut while scrubbing or under reduced motion.

### WS3 — fallback parity
- [x] Bundled `/generated/film-NN.mp4` scrubs identically with `live=false`
      (single-segment timeline; `currentTime = fraction·duration`).

### Verification
- [x] `pnpm --filter @kinora/desktop typecheck && build` green (all new files).
- [x] Runtime harness (`__demo__/`) + Electron verifier: **14/14** — scrub frame-
      accurate, within-segment tracking, ~60fps (median 8.3ms) under continuous
      scroll, segment handoff, crossfade, reduced-motion instant cuts, no errors.
- [x] `test:reading` green — **27/27** (added `computeFrame`, `scrollVelocity`).

### Code review (peer, fresh-eyes) — done
- [x] BLOCKING `onSeeked` re-seek yank → reproduced + fixed (reveal-only `onReady`).
- [x] Blank-pane on overlapping crossfades → `shownKey` fallback.
- [x] Flick interrupting a crossfade → hard-cut to active layer.
- [x] Reviewer verified clean: timeline math, 60fps architecture, scheduler parity,
      lane, rAF lifecycle. See VERIFICATION.md.

### Done
- [x] CONTRACTS.md matches the shipped signature · artifacts in `coordination/artifacts/agent-02/`.
- [x] typecheck + build green; all DoD items pass → output the completion promise.

## Notes / decisions
- `coordination/artifacts/agent-04/` in the mission DoD is template residue — this
  agent's artifacts live in `coordination/artifacts/agent-02/` (corrected).
- Engine decoupled from `lib/api.ts` / `src/motion/` / event-film API via props +
  pure `timeline.ts`, so it builds green on `main` today. Seams in `requests/agent-04.md`.
