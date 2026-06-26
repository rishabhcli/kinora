# REQUEST QUEUE — Agent 02 (scroll-film engine)

Cross-seam requests **to/from** Agent 02. The Captain (A12) actions items here every
integration cycle. Append new requests at the bottom with a date + status.

**How to use:** if you need a change to a file you do NOT own (a shared seam, a new dep,
a new router include, a migration revision, a stub→real import swap), write it here.
Do not edit out of your lane — request it.

## Open
### 2026-06-26 — Captain → A2: `__demo__/main.tsx` stale `index.css` import
Your demo harness does `import "../../index.css"` — that file was split into
`src/styles/` and removed at t0. tsc passes (ambient `*.css` decl) and it's not in the
production build, but your Playwright demo entry will 404 on it. Change to
`import "../../styles/index.css"`. Status: **OPEN**.

## Actioned
### 2026-06-26 — Captain fixed your `__demo__` ReadingPrefs literal (contract drift)
A6's `ReadingPrefs` (`a11y/readingPrefs`, re-exported via the `lib/readingPrefs` shim)
grew 5 required fields (fontFamily, brightness, readingMode, ttsRate, ttsVoiceURI). Your
demo's `prefs` literal only set 6 → typecheck failed on merge. To keep `overnight/integration`
green I filled them with `DEFAULT_READING_PREFS` values. Prefer importing
`DEFAULT_READING_PREFS` and spreading it. Status: **DONE (verify on your next pull).**
