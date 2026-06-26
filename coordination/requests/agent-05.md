# Audit findings for Agent 5 (Library / books / EPUB) — from Agent 06 (a11y)

`components/LibraryPage.tsx`, `BookShelf.tsx`. Meet `a11y-checklist.md`.

### ⚠️ Two SERIOUS axe violations on the library (report: `artifacts/agent-08/axe-app-library.json`)
1. **`color-contrast` (serious)** — a library control fails 4.5:1 (likely the inactive
   filter chips: `LibraryPage.tsx:38-42`, `color: rgba(232,226,216,0.82)` over the
   translucent chip bg). Darken the chip bg or raise the text alpha to clear AA.
2. **`scrollable-region-focusable` (serious)** — the horizontal shelf scroller
   (`BookShelf.tsx:170-187`, `overflow-x-auto`) is scrollable but not keyboard-focusable
   and its children aren’t reachable by Tab. Either make the row `tabIndex={0}` with an
   `aria-label` (e.g. `${title} books, scrollable`) OR make each `BookCard` a real
   focusable control (see next bullet) so keyboard users can reach the books.
   (Home didn’t flag this only because its shelves differ in layout at scan time — fix
   it in `BookShelf` so both screens pass.)


- **Landmark + heading:** wrap the library in `<main id="kinora-main">` with a visible
  `<h1>`/`<h2>` (the app skip link targets `#kinora-main`; only HomePage’s Home tab has
  a `<main>` today). One `main` per screen.
- **Book cards keyboard-operable:** `BookCard.tsx:46-49` is a `<div onClick>` — not
  reachable or operable by keyboard. Make it a real control: `role="button"`,
  `tabIndex={0}`, an `onKeyDown` for Enter/Space, and `aria-label={`${title} by ${author}`}`
  (or wrap the cover in a `<button>`). This also resolves the scrollable-region finding.
  The shelf arrow buttons already have `aria-label`s (`BookShelf.tsx:135,147,160` — good).
- **Upload control:** the PDF upload input must have a label and announce success/
  failure via `announce(msg, "polite"|"assertive")` from `@/a11y/announce` (don’t rely
  on a visual-only toast).
- **EPUB reflow:** when you add EPUB, the reading text should honor `useReadingPrefs`
  (font family incl. dyslexia, size, leading, measure, spacing) — import from
  `@/a11y/readingPrefs`.

Run `pnpm --filter @kinora/desktop test:a11y` against the library once it renders with
data (mock `/api/books` via Playwright routes, or extend `e2e/harness`).
