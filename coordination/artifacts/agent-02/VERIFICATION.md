# Agent 02 — verification

## Definition of Done → evidence

| DoD item | How it's verified | Result |
|---|---|---|
| `typecheck && build` green | `pnpm --filter @kinora/desktop typecheck && pnpm --filter @kinora/desktop build` | green (baseline + final) |
| Pure sync-map math | 27 unit tests, `pnpm --filter @kinora/desktop test:reading` | 27/27 |
| Scrub frame-accurate to sync map (±1 shot) | runtime: `currentTime` pinned to `fraction·duration` at 0.25/0.5/0.75/0.9 (exact) | PASS |
| 60fps under fast flicks | runtime: 109 rAF frames / 0.9s of continuous scroll, **median 8.3ms, p95 9.9ms** while scrubbing+decoding | PASS |
| Cross-event handoff (WS2) | runtime: segment src swaps film-01→03→04 by word range; normal motion crossfades (2 layers→1) | PASS |
| Reduced motion = instant cuts | runtime: a src change never creates a 2nd layer under reduced motion | PASS |
| Fallback parity (WS3) | runtime: single bundled film scrubs identically with `live=false` | PASS |

Full output: [`verify-output.txt`](./verify-output.txt) — **13/13 runtime checks**.

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
