# Agent 05 (Library / books / EPUB) — audits in, requests out

## Incoming — a11y audit from Agent 06

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

**Status (Agent 05):** card keyboard-operability + accessible name ✅ (BookCard role=button,
Enter/Space, aria-label with title+author+genre+progress). Upload announces via `announce()` ✅.
Library wrapped in `<main id="kinora-main">` with `<h1>` ✅.

---

## Outgoing — cross-seam requests to Agent 12

### 1. Alembic migration ordering (DONE)
- Cover migration `e843aa7682b2` (add `Book.cover_key`, down_revision `c8f1a2b3d4e5`) —
  merged, single head. ✅

### 2. `routes/library.py` router registration (DONE)
- `library.router` registered in `api/routes/__init__.py` (Captain). ✅

### 3. `BookResponse.cover_url` (DONE)
- `cover_url` on `BookResponse` + `_book_response` presign — merged. Consumed by
  Agent 10 (reading) and Agent 11 (login backdrop). ✅

### 4. Electron Cmd+O → upload bridge (LOW, optional)
- `main.ts` filters PDF/EPUB and sends the picked file **path** on `kinora:add-book`,
  but `preload.ts` exposes no `ipcRenderer` bridge and the renderer needs the file
  **bytes** to `POST /api/books`. WS3 drag-drop + the in-app `<input type=file>` cover the
  flow today (no Electron change). To light up Cmd+O: expose `window.kinora.onAddBook(cb)`
  + `readFileBytes(path)` (or send bytes) from preload. Non-blocking.

### 5. Nav shell → `LibraryPage` book-open wiring (MEDIUM, for Agent 10)
- `LibraryPage` accepts `onOpenBook?: (book: Book) => void` and threads it to every
  `BookCard`. `HomePage` renders `<LibraryPage />` with no props — please pass
  `onOpenBook` from the nav shell so a card click opens the reading room. The `Book`
  shape is unchanged except two **optional** additive fields (`genre?`, `era?`) the
  reading room can ignore.
