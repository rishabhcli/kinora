# Kinora desktop — accessibility audit (Agent 06)

Target: **WCAG 2.2 AA** (AAA for reading text where feasible). Tooling: `@axe-core/playwright`
(real Chromium) + manual keyboard/structure review. Re-run: `pnpm --filter @kinora/desktop test:a11y`.

## Automated axe-core scan (WCAG 2.0/2.1/2.2 A + AA)

| Surface | Owner | Serious/critical | Report |
|---|---|---|---|
| Owned reading surfaces (ReadingControls + ReadAloudView) | A06 | **0** | `axe-owned-reading-surfaces.json` |
| Keyboard shortcut cheat-sheet (dialog) | A06 | **0** | `axe-owned-cheatsheet.json` |
| Login screen (`/`) | A11 | **0** (full scan) | `axe-login-full.json`, `axe-login-owned.json` |

**Owned surfaces: zero serious/critical.** Library + the live reading room render only
with the backend/data, so the owned reading-room surfaces are scanned via `e2e/harness`
(same components, real styling); per-agent findings filed for the host screens.

### Issues found + fixed during the scan
1. **`color-contrast` (serious)** — the voice `<select>` inherited light text on the UA
   white default (1.28:1). Fixed: explicit dark background in `ReadingControls`.
2. **Keyboard shortcuts swallowed by sliders** — `isTypingTarget` treated every `<input>`
   as text-entry, so `?` didn’t open the cheat-sheet while a range slider had focus.
   Fixed: only true text-entry fields (text/search/email/textarea/contenteditable…)
   suppress single-key shortcuts. (+ regression tests.)

## Keyboard-only walkthrough (recorded)
`recordings/keyboard-walkthrough.webm` — Tab → skip link → operate the Text-size slider
with arrows → open the `?` cheat-sheet (focus trapped) → Escape (focus restored). No mouse.

## Read-aloud word-sync (recorded)
`recordings/readaloud-wordsync.webm` + `wordsync-1/2.png` — the real `useTts` + `ReadAloudView`
highlight each word in lockstep with `boundary` events (here driven by a scripted speech
engine, since headless Chromium has no TTS voices; on a real Mac the same code runs against
OS voices with audible speech). Behavior is also locked by 17 unit tests.

## Manual / structural findings (filed as requests)
- **agent-10** (reading room): mount `<ReadingControls>` (replace inline popover); trap
  focus in the dialog; `useReducedMotionPref()`; mount `<ReadAloudView>` for word-sync;
  `<main id="kinora-main">` landmark.
- **agent-04** (motion): migrate 5 `useReducedMotion()` sites → `useReducedMotionPref()`.
- **agent-08** (tokens): focus-ring token, reading-text AA/AAA contrast, high-contrast +
  reduce-transparency selectors, light-on-dark control fix pattern.
- **agent-09** (nav/settings): `aria-current` on active tab; profile dropdown Escape +
  focus return; Accessibility settings section.
- **agent-05** (library), **agent-11** (login, already clean), **agent-01** (optional
  playhead→word stream for narration-synced highlighting).

## What Agent 06 shipped (owned, all green)
Centralized `src/a11y/` (reduced-motion source of truth, focus trap/restore, live-region
announcer, global keyboard layer + `?` cheat-sheet, VisuallyHidden, reading prefs incl.
dyslexia font, read-aloud engine + view) · `styles/a11y.css` (`.sr-only`, global
`:focus-visible`, reduced-motion/-transparency/-contrast + forced-colors, OpenDyslexic
`@font-face`) · `reading/ReadingControls.tsx` · `a11y-checklist.md`.
92 unit tests + 5 e2e; `typecheck` + `build` green.
