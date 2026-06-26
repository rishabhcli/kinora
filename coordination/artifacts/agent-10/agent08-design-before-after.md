# Agent 08 — Color · Depth · Typography: before / after

Screenshots captured at 1440×900 @2x against the live renderer (`vite` :5173) +
the running backend (demo library). **Before** = baseline `main@4863a0c` (the three
renderer entry files reverted, HMR-reloaded); **after** = `agent/08-design`. The
flow is identical in both — only the design tokens/material/type differ, so the
pairs are a clean A/B. Every pair differs at the byte level (verified).

| # | view | before | after |
|---|---|---|---|
| 01 | Login | `01-login-before.png` | `01-login-after.png` |
| 02 | Home | `02-home-before.png` | `02-home-after.png` |
| 03 | Library | `03-library-before.png` | `03-library-after.png` |
| 04 | Reading room — **dark** | `04-reading-dark-before.png` | `04-reading-dark-after.png` |
| 05 | Reading room — **sepia (light)** | `05-reading-sepia-before.png` | `05-reading-sepia-after.png` |

## What changed (all via tokens — no component edits, buttons unchanged in form)
- **Color** — warm-graphite neutral spine (cohesive ochre-tinted ramp) replaces the
  muddier near-blacks; evolved "lumen" brass-amber accent; tertiary "subtle" text
  lifted from a **failing** 2.8:1 to **5.1:1** (now AA for body text). Login bookwall
  + home ambient auroras now track the accent token.
- **Depth/material** — one coherent elevation ladder (specular rim + hairline +
  key/ambient shadow); cards/inputs/dock/footer re-skinned to a clean warm frosted
  material (the old multi-layer white sheen "muddy glass" is gone); degrades to
  solid under `prefers-reduced-transparency`.
- **Typography** — system-first UI (SF Pro on macOS, crisp/native) ; **Newsreader**
  introduced as a book-quality reading serif (visible in the reading pane); Fraunces
  retained + elevated for display; modular scale + measure/leading tokens.

## Buttons (mandate: unchanged in form)
Submit, social, nav, and the reading-room "Back"/swatch controls keep identical
shape, size, radius, padding and press behaviour across before/after — only
inherited token colours move. Tailwind shadow/radius/blur additions are namespaced
(`shadow-elev-*`, `rounded-k-*`, `backdrop-blur-k-*`) so no existing button utility
changed.

## How to reproduce
`pnpm --filter @kinora/desktop dev:web` (backend up), then
`node scratch/shoot.mjs after` (driver in 08's notes). Contrast gate:
`node apps/desktop/scripts/check-contrast.mjs`.
