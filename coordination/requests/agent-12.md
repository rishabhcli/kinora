# Requests for Agent 12 (Integration Captain) — from Agent 06 (a11y)

These are **shared-seam** changes Agent 06 made inside `../kinora-a06` because no
integration branch carried them yet. They are additive; please reconcile at merge.

## 1. `apps/desktop/package.json` + `pnpm-lock.yaml` — test/a11y devDeps
Added (devDependencies) to stand up the a11y test harness + DoD axe scan:
- `vitest`, `jsdom`, `@testing-library/react`, `@testing-library/jest-dom`,
  `@testing-library/user-event` — unit/hook TDD.
- `@playwright/test`, `@axe-core/playwright` — the DoD automated a11y scan.
Scripts added: `test` (`vitest run`), `test:watch`, `test:a11y` (`playwright test`).
No runtime deps changed. New config files (mine): `vitest.config.ts`,
`playwright.config.ts`, `src/test/setup.ts`.

## 2. `apps/desktop/index.html` — bundled fonts (PENDING)
Agent 06 owns the **dyslexia** font (OpenDyslexic), loaded via `src/styles/a11y.css`
(`@font-face`, not a CDN `<link>`). Coordinate with Agent 8 (UI/reading fonts) so we
don't both edit `<head>`. No change needed from you unless Agent 8 also bundles.

## 3. `apps/desktop/src/main.tsx` — mount `<A11yProvider>` + import `a11y.css` (PENDING)
The a11y layer (live-region announcer, global keyboard registry, `?` cheat-sheet,
reduced-motion/contrast/transparency `<html>` classes) mounts once, wrapping `<App/>`,
so it also covers the login screen. Also `import "@/styles/a11y.css"`. Diff will be
small + additive.

## 4. `apps/desktop/src/lib/readingPrefs.ts` — re-export shim (PENDING)
`readingPrefs.ts` moves to `src/a11y/readingPrefs.ts` (it is reading-accessibility
state). `lib/readingPrefs.ts` becomes a one-line `export * from "@/a11y/readingPrefs"`
so existing importers (today only `ReadingRoom.tsx`) keep working untouched.

_Status: item 1 landed on `agent/06-a11y`; items 2–4 land as the WS1/WS2 work merges._
