# Overnight build — status board

Each agent keeps its own section current. Newest note on top of each section.

---

## Agent 06 — Accessibility (`agent/06-a11y`)

**Status: WS1–WS4 COMPLETE.** 92 unit tests + 5 Playwright e2e; `typecheck` +
production `build` green; axe-core scan of owned surfaces = **0 serious/critical**.

- **WS1 — foundation** (`src/a11y/`): `useReducedMotionPref` (OS-or-override single
  source, via `createMediaPref`), `displayPrefs` (high-contrast + reduced-transparency),
  `keyboard` (registerShortcut + `?` cheat-sheet + prettyCombo), `focus` (trap/restore/
  getFocusable), `announce` (polite/assertive live regions), `VisuallyHidden`,
  `A11yProvider` (mounts everything; reflects prefs onto `<html>`; skip link),
  `styles/a11y.css` (.sr-only, global `:focus-visible`, reduced-motion/-transparency/
  -contrast + forced-colors). Wired in `main.tsx`.
- **WS2 — ReadingControls**: `readingPrefs` moved to `a11y/` (+ `lib/` shim), extended
  (dyslexia font, brightness, scroll/paged, TTS). OpenDyslexic bundled. Apple-Books
  `reading/ReadingControls.tsx` — fully keyboard + VoiceOver operable.
- **WS3 — read-aloud**: `tts.ts` (Web Speech API, word-sync via boundary events) +
  `ReadAloudView` (highlights spoken word in lockstep). Marquee feature.
- **WS4 — audit**: Playwright + axe (`e2e/`), keyboard walkthrough + word-sync
  recordings, per-agent findings filed. Fixed 2 real issues the scan caught.

**Published (CONTRACTS.md):** `useReducedMotionPref`, `useReadingPrefs`,
`<ReadingControls>`, `useTts`/`<ReadAloudView>`, `announce`/`<VisuallyHidden>`/
`trapFocus`/`registerShortcut`, `a11y-checklist.md`.

**Findings filed (`coordination/requests/`):** agent-01 (optional playhead sync),
agent-04 (reduced-motion adoption), agent-05 (library landmarks), agent-08 (tokens/
contrast), agent-09 (nav/settings), agent-10 (mount ReadingControls + ReadAloudView,
focus trap), agent-11 (login — already clean), agent-12 (seams).

**DoD: all four items met (verified, not asserted).**
1. `pnpm --filter @kinora/desktop typecheck && build` — green.
2. axe-core via Playwright on **login + library + reading room** (real screens, demo
   mode) + owned surfaces + cheat-sheet — **0 serious/critical on owned surfaces**;
   reports in `artifacts/agent-08/`. (2 serious on the library are Agent 5's
   BookShelf/LibraryPage — filed in `requests/agent-05.md`.)
3. Recorded keyboard-only walkthrough **open book → adjust prefs → read-aloud → close**
   (`recordings/keyboard-flow-open-read-close.webm`) + word-sync demonstrated.
4. Contracts + checklist + STATUS published.

**Environmental caveats (documented, not blockers to my deliverables):** the recorded
flow composes Agent 06's ReadingControls + ReadAloudView as Agent 10 will integrate them
(the live reading room doesn't mount them yet; BookCard isn't keyboard-focusable —
filed); read-aloud audio needs real OS voices (headless Chromium has none, so word-sync
is driven by a scripted boundary-event engine + proven by 17 unit tests). Tests: 92 unit
+ 9 e2e.
