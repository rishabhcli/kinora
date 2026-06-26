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


---

# Cross-seam requests — Agent 05 (Library) → Agent 12 (Integration)

> Changes that touch shared seams. Agent 12 reviews/merges so revisions/wiring
> don't collide. Agent 05 implements them minimally + additively in-branch and
> lists them here for awareness + conflict resolution.

## 1. Alembic migration ordering (HIGH)
- Agent 05 adds **one** migration: `add Book.cover_key` with
  `down_revision = "c8f1a2b3d4e5"` (the current head at branch time).
- If another agent (07/12) also bases a migration on `c8f1a2b3d4e5`, we get
  **multiple heads**. Please serialize at integration (re-point my
  `down_revision`, or `alembic merge`). My migration is purely additive
  (`ADD COLUMN books.cover_key VARCHAR(1024) NULL`) so reordering is safe.

## 2. `routes/library.py` router registration (MEDIUM)
- New router `app.api.routes.library:router` (prefix `/books`, the
  `GET /{book_id}/cover` endpoint, plus future library niceties).
- Needs `app.include_router(library.router, prefix=API_PREFIX)` in
  `backend/app/main.py` `ROUTERS`. Implemented additively in-branch; flagging
  in case ROUTERS is re-ordered upstream.

## 3. `BookResponse.cover_url` (MEDIUM)
- Added `cover_url: str | None = None` to `app/api/schemas.py:BookResponse` and
  populated it in `routes/books.py:_book_response` (presign `book.cover_key`).
  Both are shared seams. Change is additive and minimal (one field + one presign).
  Agent 10 (reading) and Agent 11 (login backdrop) consume `cover_url`.

## 4. Electron Cmd+O → upload bridge (LOW, optional)
- `main.ts` already filters PDF/EPUB and sends the picked file **path** on the
  `kinora:add-book` channel, but `preload.ts` exposes no `ipcRenderer` bridge, and
  the renderer needs the file **bytes** to `POST /api/books`. WS3 drag-drop + the
  in-app `<input type=file>` cover the flow today (no Electron change). To also
  light up Cmd+O: expose `window.kinora.onAddBook(cb)` + a `readFileBytes(path)`
  (or send bytes directly) from preload. Non-blocking.

## 5. Nav shell → `LibraryPage` book-open wiring (MEDIUM, for Agent 10)
- `LibraryPage` now accepts `onOpenBook?: (book: Book) => void` and threads it to
  every `BookCard`. `HomePage` currently renders `<LibraryPage />` with no props.
  Please pass `onOpenBook` from the nav shell so a card click opens the reading
  room. The `Book` shape is unchanged except two **optional** additive fields
  (`genre?`, `era?`) the reading room can ignore.
