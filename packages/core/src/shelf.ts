/**
 * Shelf helpers — import gating and live progress labels shared by desktop
 * and mobile library screens (§5.1).
 */
import type { BookResponse } from "./api/types";

/** Strip internal seed/dev suffixes from titles shown in the product UI. */
export function displayBookTitle(title: string): string {
  return title.replace(/\s*\((e2e seed|dev seed)\)\s*$/i, "").trim();
}

/** Human-readable import stage for status chips (sentence case). */
export function stageLabel(book: Pick<BookResponse, "status" | "stage">): string {
  if (book.status === "failed") return "Import failed";
  const stage = book.stage?.trim();
  if (stage) return stage.charAt(0).toUpperCase() + stage.slice(1).replace(/[_-]+/g, " ");
  return "Preparing";
}

/** Progress 0–100 for UI bars; null when unknown. */
export function progressPercent(book: Pick<BookResponse, "progress">): number | null {
  if (book.progress == null || Number.isNaN(book.progress)) return null;
  return Math.min(100, Math.max(0, Math.round(book.progress * 100)));
}

/** Block opening a book that is not ready; null means safe to open. */
export function importGateMessage(book: BookResponse): string | null {
  if (book.status === "ready") return null;
  if (book.status === "failed") {
    return "This import failed. Remove the book and try uploading again.";
  }
  const pct = progressPercent(book);
  const stage = stageLabel(book);
  if (pct != null) return `${stage} — ${pct}% complete. The film will be ready soon.`;
  return `${stage}… Kinora is still adapting this book.`;
}

/** True when the shelf should keep polling / listening for ingest updates. */
export function shelfNeedsIngestUpdates(books: BookResponse[] | undefined): boolean {
  return (books ?? []).some((b) => b.status === "importing");
}

/** Merge a live ingest_progress payload into a book row. */
export function applyIngestProgress(
  book: BookResponse,
  update: { stage?: string; pct?: number },
): BookResponse {
  return {
    ...book,
    status: book.status === "ready" ? "ready" : "importing",
    stage: update.stage ?? book.stage,
    progress: update.pct ?? book.progress,
  };
}
