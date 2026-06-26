# Overnight build — status board

Each agent keeps its own section current. Newest note on top of each section.

---

## Agent 06 — Accessibility (`agent/06-a11y`)

**Now:** Setup complete. Worktree `../kinora-a06` on `agent/06-a11y` (base
`overnight/integration`). Coordination scaffolding + contract signatures published.
Mapping the renderer; starting WS1 (a11y foundation) with TDD.

**Plan:**
- WS1 — `src/a11y/`: `useReducedMotionPref` (OS + in-app override, single source),
  `keyboard.ts` (registerShortcut + `?` cheat-sheet), `focus.ts` (trap/restore),
  `announce.ts` (live region), `VisuallyHidden.tsx`, `a11y.css` (reduced-motion /
  -transparency / -contrast, focus rings, high-contrast theme, `.sr-only`).
- WS2 — move `readingPrefs.ts` → `a11y/` (+ `lib/` shim); extend prefs (dyslexia
  font, brightness, scroll/paged, TTS); build `ReadingControls.tsx`.
- WS3 — `tts.ts` read-aloud with word-synced highlighting (Web Speech API).
- WS4 — SR/keyboard pass on owned surfaces; full-app axe + manual audit; file
  per-agent findings in `coordination/requests/agent-XX.md`.

**Needs from others (stubbed against contracts until merged):**
- Agent 8: theme/color tokens (contrast, high-contrast theme, dyslexia-safe defaults).
- Agent 1: playhead / `focusWord` for read-aloud word-sync.
- Agent 10: reading-room shell slot where `<ReadingControls>` mounts.

**Seam requests filed:** see `coordination/requests/` (package.json devDeps for
testing/a11y libs; `index.html` font; `lib/readingPrefs.ts` shim; `main.tsx` providers).
