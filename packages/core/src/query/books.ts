import type { BookResponse } from "../api/types";

/** True while any book on the shelf is still importing — drives poll interval. */
export function booksNeedPolling(books: BookResponse[] | undefined): boolean {
  return books?.some((b) => b.status === "importing") ?? false;
}

/** Poll the shelf every 2s while imports are in flight (§5.1 ingest progress). */
export const BOOKS_POLL_MS = 2_000;

/** React Query `refetchInterval` callback — poll while any title is importing. */
export function booksRefetchInterval(books: BookResponse[] | undefined): number | false {
  return booksNeedPolling(books) ? BOOKS_POLL_MS : false;
}

/** A short, human label for a book that isn't ready yet. */
export function bookStageLabel(book: BookResponse): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** Clamp ingest progress to 0–100 for UI, or null when unknown. */
export function bookProgressPercent(progress: number | null | undefined): number | null {
  if (progress == null || Number.isNaN(progress)) return null;
  const pct = progress <= 1 ? progress * 100 : progress;
  return Math.max(0, Math.min(100, Math.round(pct)));
}

/** Whether the reader can open this book in the workspace. */
export function bookIsOpenable(book: BookResponse): boolean {
  return book.status === "ready";
}
