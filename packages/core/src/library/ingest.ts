/**
 * Shelf-side helpers for Phase-A ingest progress (§5.1).
 */
import { type BookResponse } from "../api/types";
import { type KinoraEvent } from "../events";

/** Map an SSE `ingest_progress` payload onto a shelf row. */
export function applyIngestProgress(
  book: BookResponse,
  payload: { book_id: string; stage?: unknown; pct?: unknown },
): BookResponse {
  if (book.id !== payload.book_id) return book;
  const stage = typeof payload.stage === "string" ? payload.stage : book.stage;
  const pct = typeof payload.pct === "number" ? payload.pct : book.progress;
  return { ...book, stage, progress: pct };
}

/** Patch the React Query books list when an ingest event arrives. */
export function patchBooksWithIngestEvent(
  books: BookResponse[] | undefined,
  event: KinoraEvent,
): BookResponse[] | undefined {
  if (!books || event.event !== "ingest_progress") return books;
  return books.map((book) => applyIngestProgress(book, event));
}

/** True when any book on the shelf is still importing. */
export function shelfHasImporting(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some((b) => b.status === "importing");
}

/** Human label for a book that isn't ready yet. */
export function bookStageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** Progress percentage (0–100) for display, or null when unknown. */
export function bookProgressPercent(book: BookResponse): number | null {
  const raw = book.progress;
  if (typeof raw !== "number" || Number.isNaN(raw)) return null;
  return Math.round(Math.min(1, Math.max(0, raw)) * 100);
}
