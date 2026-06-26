# Kinora overnight build — CONTRACTS

> What each agent PUBLISHES for others to consume. Keep additive and stable.

## Agent 05 — Library / books / covers

### Backend (consumed by Agent 10 reading, Agent 11 login backdrop)

- `Book.cover_key: str | None` — object-store key of the book's cover image
  (`covers/{book_id}`), set by the seeder / `fetch_hd_covers.py` / EPUB upload.
- `BookResponse.cover_url: str | None` — presigned GET URL for `cover_key`
  (added additively; `null` when a book has no cover yet). Present on every
  `GET /api/books` and `GET /api/books/{id}` response.
- `GET /api/books/{id}/cover` — authed + ownership-checked; **302 redirect** to the
  presigned cover URL (or 404 if the book has no cover). Stable accessor for
  native shells / `<img>` that can resolve via the BookResponse `cover_url`.

### Catalogue manifest

- `assets/books/catalog.json` — array of `{ id, gutenberg_id, title, author,
  genre, era, tags[], cover_source, source }`. The Retrieval/Understanding
  manifest the seeder writes; safe for other agents to read for metadata.

### Desktop client (`apps/desktop/src/lib/api/library.ts`)

Built on the shared base client (`lib/api.ts`) without modifying it:

- `listLibrary(): Promise<LibraryBook[]>` — the user's shelf, newest first, with
  HD `coverImage` (from `cover_url`) and catalogue `genre`/`era` joined by id.
- `uploadBook(file, fields?): Promise<LibraryBook>` — POST a PDF/EPUB.
- `pollBookUntilReady(id, onUpdate, opts?)` — poll ingest status to ready.
- `LibraryBook = Book & { genre?, era? }` (the optional fields are additive on the
  shared `Book`; the reading room ignores them).
- Pure helpers: `searchBooks`, `sortBooks(key)`, `shelvesFor` (Continue Reading +
  genre shelves + "Your Library"), plus `CATALOG_GENRES`.

`<BookCard>` is keyboard-openable (role=button, Enter/Space, aria-label) and shows
a genre tag. `<UploadBook>` is drag-drop + click, validating type/size/page-cap
with friendly errors and surfacing optimistic placeholders via `onUploadsChange`.

### Catalogue (renderer)

- `apps/desktop/src/data/catalog.ts` — `CATALOG_META` (id → genre/era/tags) +
  `CATALOG_GENRES`, auto-generated from `assets/books/catalog.json`.

_Statuses/shapes finalize as the work lands; this section is updated per commit._
