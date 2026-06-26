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

- `listLibrary()` → `Book[]` (real backend, cover_url-bearing) — to be published.
- `uploadBook(file, opts)` → upload + ingest-status polling — to be published.

_Statuses/shapes finalize as the work lands; this section is updated per commit._
