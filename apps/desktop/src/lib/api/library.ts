// Library client (Agent 05) — builds on the shared base client (`lib/api.ts`,
// owned by Agent 10) without modifying it: the real backend shelf, enriched with
// HD covers (`cover_url`) and the catalogue's genre/era metadata, plus the
// "upload your own EPUB" flow and ingest-status polling.
import { api, toBrowserUrl, toUiBook, type BookResponse } from "../api";
import { CATALOG_GENRES, CATALOG_META } from "../../data/catalog";
import type { Book } from "../../data/books";

export { CATALOG_GENRES };

/** Backend book response widened with Agent 05's additive `cover_url` field.
 *  (The base `BookResponse` type in `lib/api.ts` gains this once Agent 10 lands
 *  it; until then we widen locally — the runtime payload already carries it.) */
export interface LibraryBookResponse extends BookResponse {
  cover_url?: string | null;
}

/** A UI book enriched with catalogue genre/era (for shelves + the genre tag). */
export interface LibraryBook extends Book {
  genre?: string;
  era?: string;
}

/** Map a backend book onto the enriched UI shape: HD cover from `cover_url`
 *  (presigned → browser-reachable, gradient fallback when absent), genre/era
 *  joined from the bundled catalogue manifest by stable book id. */
export function toLibraryBook(b: LibraryBookResponse): LibraryBook {
  const ui = toUiBook(b, toBrowserUrl(b.cover_url ?? ""));
  const meta = CATALOG_META[b.id];
  // `b.progress` is ingest/generation progress (1.0 once READY), NOT reading
  // progress — the shelf must not paint a "100% read" ring on an unread book.
  // Reading position comes from sessions (Agent 2/10); until a READY book has a
  // distinct reading-progress field, the shelf shows it as unread (0).
  const progress = b.status === "ready" ? 0 : ui.progress;
  return { ...ui, progress, genre: meta?.genre, era: meta?.era };
}

/** The current user's full library (newest first), enriched. */
export async function listLibrary(): Promise<LibraryBook[]> {
  const rows = (await api.listBooks()) as LibraryBookResponse[];
  return rows.map(toLibraryBook);
}

export interface UploadFields {
  title?: string;
  author?: string;
  art_direction?: string;
}

/** Upload a PDF/EPUB; resolves to the freshly-created (importing) book. */
export async function uploadBook(file: File, fields: UploadFields = {}): Promise<LibraryBook> {
  const created = (await api.uploadBook(file, fields)) as LibraryBookResponse;
  return toLibraryBook(created);
}

/** Poll a book until it leaves the importing/error states, surfacing each tick
 *  (status/progress/stage) so the shelf placeholder can animate to ready. */
export async function pollBookUntilReady(
  id: string,
  onUpdate: (book: LibraryBook) => void,
  opts: { intervalMs?: number; timeoutMs?: number } = {},
): Promise<LibraryBook> {
  const intervalMs = opts.intervalMs ?? 1500;
  const timeoutMs = opts.timeoutMs ?? 5 * 60_000;
  const deadline = Date.now() + timeoutMs;
  // eslint-disable-next-line no-constant-condition
  while (true) {
    const book = toLibraryBook((await api.getBook(id)) as LibraryBookResponse);
    onUpdate(book);
    const status = book.isNew ? "importing" : "ready";
    if (!book.isNew || status === "ready") return book;
    if (Date.now() > deadline) return book;
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

// ---- Pure shelf helpers (search / sort / group) -------------------------- //

export type SortKey = "recent" | "title" | "author" | "progress";

export function searchBooks(books: LibraryBook[], query: string): LibraryBook[] {
  const q = query.trim().toLowerCase();
  if (!q) return books;
  return books.filter(
    (b) => b.title.toLowerCase().includes(q) || b.author.toLowerCase().includes(q),
  );
}

export function sortBooks(books: LibraryBook[], key: SortKey): LibraryBook[] {
  const copy = [...books];
  switch (key) {
    case "title":
      return copy.sort((a, b) => a.title.localeCompare(b.title));
    case "author":
      return copy.sort((a, b) => a.author.localeCompare(b.author));
    case "progress":
      return copy.sort((a, b) => b.progress - a.progress);
    case "recent":
    default:
      return copy; // listLibrary already returns newest-first
  }
}

export interface Shelf {
  title: string;
  books: LibraryBook[];
}

/** Group enriched books into shelves: Continue Reading (progress>0) first, then
 *  one shelf per genre (catalogue order), then a catch-all for uploads/unknowns. */
export function shelvesFor(books: LibraryBook[]): Shelf[] {
  const shelves: Shelf[] = [];
  const continueReading = books.filter((b) => b.progress > 0 && b.progress < 100);
  if (continueReading.length) shelves.push({ title: "Continue Reading", books: continueReading });

  const byGenre = new Map<string, LibraryBook[]>();
  const uncatalogued: LibraryBook[] = [];
  for (const b of books) {
    if (b.genre) {
      const list = byGenre.get(b.genre) ?? [];
      list.push(b);
      byGenre.set(b.genre, list);
    } else {
      uncatalogued.push(b);
    }
  }
  for (const genre of CATALOG_GENRES) {
    const list = byGenre.get(genre);
    if (list && list.length) shelves.push({ title: genre, books: list });
  }
  if (uncatalogued.length) shelves.push({ title: "Your Library", books: uncatalogued });
  return shelves;
}
