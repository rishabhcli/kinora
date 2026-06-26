# Audit findings for Agent 5 (Library / books / EPUB) — from Agent 06 (a11y)

`components/LibraryPage.tsx`, `BookShelf.tsx`. Meet `a11y-checklist.md`.

- **Landmark + heading:** wrap the library in `<main id="kinora-main">` with a visible
  `<h1>`/`<h2>` (the app skip link targets `#kinora-main`; only HomePage’s Home tab has
  a `<main>` today). One `main` per screen.
- **Book cards keyboard-operable:** card actions are buttons with `aria-label`s
  (`BookShelf.tsx:135,147,160` — good). Ensure the whole card (or its primary action)
  is reachable by Tab and has a clear accessible name (title + author), and a visible
  focus ring (the global `:focus-visible` now provides one — don’t suppress it).
- **Upload control:** the PDF upload input must have a label and announce success/
  failure via `announce(msg, "polite"|"assertive")` from `@/a11y/announce` (don’t rely
  on a visual-only toast).
- **EPUB reflow:** when you add EPUB, the reading text should honor `useReadingPrefs`
  (font family incl. dyslexia, size, leading, measure, spacing) — import from
  `@/a11y/readingPrefs`.

Run `pnpm --filter @kinora/desktop test:a11y` against the library once it renders with
data (mock `/api/books` via Playwright routes, or extend `e2e/harness`).
