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


---

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
