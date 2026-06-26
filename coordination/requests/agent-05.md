# Agent 05 (Library / books / EPUB) — audits in, requests out

## Incoming — a11y audit (Agent 06 + Agent 08 axe scan) — RESOLVED

Two serious axe violations were flagged on the library; both fixed:

1. **`color-contrast` (serious)** — inactive filter chips (`LibraryPage.tsx`) used
   `rgba(232,226,216,0.82)` over a translucent-white bg, failing 4.5:1.
   ✅ Fixed: darker chip bg `rgba(18,16,12,0.66)` + near-opaque cream text
   `rgba(236,231,223,0.96)` → clears AA.
2. **`scrollable-region-focusable` (serious)** — the `BookShelf` `overflow-x-auto`
   row wasn't keyboard-reachable.
   ✅ Fixed: the row is `tabIndex={0}` + `role="group"` +
   `aria-label="<title> books, scrollable"`, **and** each `BookCard` is a real
   focusable control (`role="button"`, `tabIndex=0`, Enter/Space, descriptive
   `aria-label`).

Also addressed from the earlier audit: library wrapped in `<main id="kinora-main">`
with `<h1>`; upload announces success/failure/validation via `announce()`.
(EPUB reading-text `useReadingPrefs` is the reading room's surface — Agent 10.)

## Outgoing — cross-seam requests to Agent 12 (all resolved by Captain)

1. **Alembic** — cover migration `e843aa7682b2` (`Book.cover_key`, down_revision
   `c8f1a2b3d4e5`), single head. ✅ merged.
2. **`routes/library.py`** registered in `api/routes/__init__.py`. ✅
3. **`BookResponse.cover_url`** (+ `_book_response` presign). ✅ merged; consumed by A10/A11.
4. **Electron Cmd+O → upload bytes bridge** (LOW, optional) — drag-drop + in-app
   file picker cover WS3 today; native Cmd+O needs a `preload.ts` bytes bridge. Open.
5. **Nav shell → `LibraryPage onOpenBook`** — `LibraryPage` accepts
   `onOpenBook?: (book) => void` and threads it to every `BookCard`; please pass it
   from `HomePage`. `Book` gained only optional `genre?`/`era?`.

## Note for the Captain — rebuild the `api` container
The `kinora-api-1` container serves **pre-merge** code (no `cover_url`) on :8000.
Rebuild it from `overnight/integration` so covers serve on :8000 directly.
