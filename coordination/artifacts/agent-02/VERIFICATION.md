# Agent 02 — verification

## Scope notes (honest accounting)

- **Mounting:** the engine is published and verified in isolation (demo harness);
  it is **not yet wired into `ReadingRoom.tsx`** — that's Agent 12's integration
  seam (the mission forbids me touching the ReadingRoom shell). Contract +
  wiring notes are in `coordination/CONTRACTS.md`.
- **Inertia / parallax / scrub indicator** (WS1): done — inertia is native momentum
  on the overflow scroller; parallax + scrub indicator are imperative GPU transforms.
- **Scroll-snap to scene boundaries** (named in WS1 prose, *not* in the numbered DoD
  acceptance): **deliberately deferred.** Two reasons: (1) there's no reliable
  scene→DOM anchor — shots carry book-global `word_range`s while the text column is
  page text, so snap points would be guesses; (2) `scroll-snap` nudges
  programmatic/settle scroll positions, which fights frame-accurate scrub pinning.
  Native inertia covers the "settle" feel without that risk. Revisit if Agent 5
  exposes per-paragraph word offsets.

## Definition of Done → evidence

| DoD item | How it's verified | Result |
|---|---|---|
| `typecheck && build` green | `pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/desktop build` | green (baseline + final) |
| Pure sync-map math | 27 unit tests, `pnpm --filter @kinora/desktop test:reading` | 27/27 |
| Scrub frame-accurate to sync map (±1 shot) | runtime: `currentTime` pinned to `fraction·duration` at 0.25/0.5/0.75/0.9 (exact) | PASS |
| Scrub *within* one film tracks (no re-seek yank) | runtime: scrub a film to t=3.0 → `currentTime`=3.00 (regression guard, see review below) | PASS |
| 60fps under fast flicks | runtime: 109 rAF frames / 0.9s of continuous scroll, **median 8.3ms, p95 ~10ms** while scrubbing+decoding | PASS |
| Cross-event handoff (WS2) | runtime: segment src swaps film-01→03→04 by word range; normal motion crossfades (2 layers→1) | PASS |
| Reduced motion = instant cuts | runtime: a src change never creates a 2nd layer under reduced motion | PASS |
| Fallback parity (WS3) | runtime: single bundled film scrubs identically with `live=false` | PASS |

Full output: [`verify-output.txt`](./verify-output.txt) — **14/14 runtime checks**.

## Code review (peer) outcome

A fresh-eyes reviewer traced the FilmPane state machine + rAF loop and ran the
tests. Findings and resolution:

- **BLOCKING — `onSeeked → onReady` re-seeked to a stale `pendingTime`.** `onSeeked`
  fires on *every* seek, including the rAF loop's live scrub seeks, so scrubbing
  *within* a freshly-entered live segment was yanked back to the entry frame. The
  fallback verifier missed it (single src ⇒ the re-seek path stayed dormant).
  Reproduced deterministically via the FilmPane probe (scrub→3.0 read `currentTime`
  0.00), then fixed: `onReady` is now reveal-only; `currentTime` is owned solely by
  `applyActive`, initial play by `autoPlay`. New regression check is green (3.00).
- **Real, edge-path — momentary blank pane** when two src changes land inside one
  crossfade window: `revealKey` pointed at an evicted layer ⇒ all layers opacity 0.
  Fixed with a `shownKey` fallback (reveal the oldest mounted layer when `revealKey`
  is absent), so a layer is always visible.
- **Minor — a flick interrupting a settle-crossfade** now hard-cuts to the active
  layer (scrub is always single-layer/instant) instead of finishing the fade.
- Verified clean by the reviewer: timeline math, the 60fps no-per-frame-setState
  architecture, scheduler signalling parity, lane adherence, rAF lifecycle/cleanup.

## Why Electron (not Playwright)

The bundled films are H.264. Playwright's bundled Chromium has no proprietary
codecs, and installing the `chrome` channel needs sudo here. Electron (already a
desktop devDep) ships Chromium **with** H.264, so the verifier drives a real
`<video>` and asserts actual `currentTime`/decode — not a stub.

## Reproduce

```bash
cd ../kinora-a02            # the worktree
pnpm install --filter @kinora/desktop...
pnpm --filter @kinora/desktop test:reading            # pure math (no browser)

# Runtime (needs the dev server + Electron's chromium):
pnpm --filter @kinora/desktop exec vite --port 5199 --strictPort &
node_modules/.pnpm/electron@*/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron \
  apps/desktop/src/reading/__demo__/electron-verify.cjs
```

The demo pages are dev-only (`src/reading/__demo__/`) and are never referenced by
the production `index.html`, so they don't enter the `vite build` output.

## 60fps — note on the claim

The hot path takes **no React state per frame**: scroll → `currentTime`, parallax
`translate3d`, the rail, and the scrub indicator are all written imperatively to
refs inside one `requestAnimationFrame` loop; only `transform`/`opacity` animate.
React re-renders only on structural change (a film `src` boundary, the timeline
loading, reduced-motion toggling). The measured cadence above corroborates this.
