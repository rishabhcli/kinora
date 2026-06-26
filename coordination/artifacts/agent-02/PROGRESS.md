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
- [ ] `useScrollFilm.ts`: rAF scroll→currentTime, velocity scrub/play, scheduler
      signalling (postIntent/seek), at-rest debounce, preload, parallax refs.
- [ ] `ScrollFilmEngine.tsx`: scroll container + text column + film pane + rail +
      scrub indicator + parallax. Inertia / scroll-snap. GPU transforms only.

### WS2 — cross-event handoff
- [ ] `FilmPane.tsx`: ≤2 `<video>` layers, crossfade only on `src` change
      (event-level when stitched films exist), instant under reduced motion.

### WS3 — fallback parity
- [ ] Bundled `/generated/film-NN.mp4` scrubs identically with `live=false`.

### Verification
- [ ] `pnpm --filter @kinora/desktop typecheck && build` green (with all new files).
- [ ] Runtime demo harness (`__demo__/`) + Playwright: scrub tracks scroll;
      reduced-motion = instant cuts; no console errors; rAF-driven (no jank).
- [ ] `test:reading` green for any added pure helpers.

### Done
- [ ] CONTRACTS.md final · artifacts in `coordination/artifacts/agent-02/` · self code-review.
- [ ] Output `<promise>AGENT 02 COMPLETE</promise>` only when all DoD items truly pass.

## Notes / decisions
- `coordination/artifacts/agent-04/` in the mission DoD is template residue — this
  agent's artifacts live in `coordination/artifacts/agent-02/` (corrected).
- Engine decoupled from `lib/api.ts` / `src/motion/` / event-film API via props +
  pure `timeline.ts`, so it builds green on `main` today. Seams in `requests/agent-04.md`.
