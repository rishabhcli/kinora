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

**Remaining (needs the real app on macOS / other agents merged):** live-voice
read-aloud audio (headless Chromium has no voices — word-sync logic proven by tests +
scripted-engine recording); axe on the *live* library/reading-room (need backend data +
Agents 5/10 merged — owned surfaces covered via `e2e/harness`).
