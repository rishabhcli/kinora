# Scroll Film Engine — dev-only harness

Not part of the app. The production `index.html` never references these files, so
`vite build` doesn't bundle them; `tsconfig` still typechecks the `.tsx`.

- `scrub-demo.html` / `main.tsx` — mounts `ScrollFilmEngine` with synthetic data.
  `?mode=fallback` (single bundled film, `live=false`) · `?mode=live` (4 shots → 4
  clips) · `?reduce=1` (reduced motion). Exposes `window.__kinora.read()`.
- `filmpane-probe.html` / `filmpane-probe.tsx` — mounts `FilmPane` alone and exposes
  its imperative handle on `window.__pane` so layer behaviour (crossfade vs. instant
  cut) can be driven directly. `?reduce=1` forces reduced motion.
- `electron-verify.cjs` — the runtime verifier. Drives the two pages through
  Electron's Chromium (real H.264 decode) and asserts scrub accuracy, 60fps frame
  cadence, segment handoff, crossfade, and reduced-motion instant cuts.

Run from the repository root:

```bash
pnpm --filter @kinora/desktop exec vite --port 5199 --strictPort &
node_modules/.pnpm/electron@*/node_modules/electron/dist/Electron.app/Contents/MacOS/Electron \
  apps/desktop/src/reading/__demo__/electron-verify.cjs
```
